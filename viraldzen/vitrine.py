"""Publish one topic card to the V-plan vitrine tab «План +»."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

PLAN_PLUS_TAB = "План +"
INBOX_PATH = "data/inbox.json"
PUBLISHED_PATHS = ("data/published.json", "published.json")
DEFAULT_REMOTE_ROOT = "~/Vplanist"

CARD_KEYS = ("title", "hook", "source_url", "source_date", "date", "why")
FORBIDDEN_CARD_KEYS = frozenset(
    {
        "queue",
        "queue_no",
        "queue_number",
        "article",
        "article_no",
        "article_number",
        "article_id",
        "number",
        "order",
        "position",
        "номер",
        "номер_очереди",
        "номер_статьи",
    }
)
PREFERRED_ARRAY_KEYS = frozenset(
    {"inbox", "items", "cards", "topics", "queue", "plan", "planPlus", "plan_plus"}
)
EXCLUDED_ARRAY_KEYS = frozenset({"articles", "published", "html", "www", "статьи"})
EXCLUDED_LABELS = frozenset({"статьи", "статья"})
TITLE_KEYS = ("title", "заголовок", "headline")
URL_KEYS = ("source_url", "url", "link", "source")
ADDED_AT_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2} МСК$")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]+$|^(?:\d{1,3}\.){3}\d{1,3}$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_ROOT_RE = re.compile(r"^(?:~|/)?(?:/[A-Za-z0-9._-]+)+$")


class VitrineError(Exception):
    """A safe, user-facing refusal. The message must not contain secrets."""


def now_moscow(moment: datetime | None = None) -> str:
    current = moment or datetime.now(ZoneInfo("Europe/Moscow"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Europe/Moscow"))
    else:
        current = current.astimezone(ZoneInfo("Europe/Moscow"))
    return current.strftime("%d.%m.%Y %H:%M МСК")


def build_card(
    *,
    title: str,
    hook: str,
    source_url: str,
    source_date: str,
    why: str,
    added_at: str,
) -> dict[str, str]:
    fields = {
        "title": title,
        "hook": hook,
        "source_url": source_url,
        "source_date": source_date,
        "date": added_at,
        "why": why,
    }
    missing = [key for key, value in fields.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise VitrineError("В карточке пустые поля: " + ", ".join(missing))
    if not ADDED_AT_RE.fullmatch(added_at.strip()):
        raise VitrineError("Поле date должно быть моментом добавления: ДД.ММ.ГГГГ ЧЧ:ММ МСК")
    card = {key: fields[key].strip() for key in CARD_KEYS}
    if not card["source_url"].startswith(("https://", "http://")):
        raise VitrineError("source_url должен быть ссылкой на источник")
    return card


def card_from_mapping(raw: Mapping[str, object], *, added_at: str) -> dict[str, str]:
    forbidden = sorted(FORBIDDEN_CARD_KEYS.intersection(raw))
    if forbidden:
        raise VitrineError(
            "Номер очереди и номер статьи ставит диспетчер. Уберите поля: " + ", ".join(forbidden)
        )
    def text(key: str) -> str:
        value = raw.get(key, "")
        return value if isinstance(value, str) else ""

    return build_card(
        title=text("title"),
        hook=text("hook"),
        source_url=text("source_url"),
        source_date=text("source_date"),
        why=text("why"),
        added_at=added_at,
    )


def norm_title(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def norm_url(value: str) -> str:
    text = value.strip().casefold()
    text = text.split("#", 1)[0]
    if "?" in text:
        base, query = text.split("?", 1)
        kept = [part for part in query.split("&") if part and not part.startswith("utm_")]
        text = base if not kept else base + "?" + "&".join(kept)
    if text.endswith("/"):
        text = text[:-1]
    return text


def _card_title(obj: Mapping[str, object]) -> str:
    for key in TITLE_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _card_url(obj: Mapping[str, object]) -> str:
    for key in URL_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value
    return ""


def iter_topic_cards(data: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if _card_title(node) or _card_url(node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def find_duplicate(
    card: Mapping[str, str],
    inbox_data: object,
    published_data: object,
) -> str | None:
    title = norm_title(card["title"])
    url = norm_url(card["source_url"])
    for source_name, data in (("очереди", inbox_data), ("опубликованных", published_data)):
        for existing in iter_topic_cards(data):
            existing_title = norm_title(_card_title(existing))
            existing_url = norm_url(_card_url(existing))
            if title and existing_title and title == existing_title:
                return f"Такая тема уже есть среди {source_name}"
            if url and existing_url and url == existing_url:
                return f"Такая ссылка источника уже есть среди {source_name}"
    return None


def is_single_insertion(old: str, new: str) -> bool:
    """True when `new` is `old` plus one inserted span and nothing else changed."""
    if len(new) <= len(old):
        return False
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    rest = old[prefix:]
    if rest == "":
        return True
    return new.endswith(rest) and len(new) - len(rest) >= prefix


def choose_array_path(data: object) -> tuple[str, ...]:
    arrays: list[dict[str, object]] = []

    def label_of(node: dict) -> str:
        for key in ("name", "title", "tab", "label"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def walk(node: object, path: tuple[str, ...], inside_plan: bool, excluded: bool) -> None:
        if isinstance(node, dict):
            label = label_of(node)
            next_plan = inside_plan or label == PLAN_PLUS_TAB
            next_excluded = excluded or label.casefold() in EXCLUDED_LABELS
            for key, value in node.items():
                key_text = str(key)
                child_plan = next_plan or key_text == PLAN_PLUS_TAB
                child_excluded = next_excluded or key_text.casefold() in EXCLUDED_ARRAY_KEYS
                walk(value, path + (key_text,), child_plan, child_excluded)
            return
        if isinstance(node, list) and all(isinstance(item, dict) for item in node):
            cardish = not node or any(_card_title(item) or _card_url(item) for item in node)
            arrays.append(
                {
                    "path": path,
                    "plan": inside_plan,
                    "excluded": excluded,
                    "cardish": cardish,
                }
            )
            for index, item in enumerate(node):
                walk(item, path + (str(index),), inside_plan, excluded)

    walk(data, (), False, False)
    usable = [item for item in arrays if item["cardish"] and not item["excluded"]]
    plan_arrays = [item for item in usable if item["plan"]]
    pool = plan_arrays or usable
    preferred = []
    for item in pool:
        path = item["path"]
        if not isinstance(path, tuple) or not path or path[-1] not in PREFERRED_ARRAY_KEYS:
            continue
        top_level = len(path) == 1
        if top_level or item["plan"]:
            preferred.append(item)
    if preferred:
        pool = preferred
    if len(pool) == 1:
        path = pool[0]["path"]
        if isinstance(path, tuple):
            return path
    if not pool:
        raise VitrineError("В inbox.json нет списка карточек «План +». Файл не меняю.")
    raise VitrineError("В inbox.json несколько списков карточек. Файл не меняю.")


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _scan_string(text: str, index: int) -> tuple[str, int]:
    if text[index] != '"':
        raise VitrineError("Ожидалась строка в inbox.json")
    index += 1
    chars: list[str] = []
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(chars), index + 1
        if char == "\\":
            index += 1
            if index >= len(text):
                break
            escaped = text[index]
            mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
            if escaped in mapping:
                chars.append(mapping[escaped])
                index += 1
                continue
            if escaped == "u":
                hex_digits = text[index + 1 : index + 5]
                if len(hex_digits) != 4 or any(digit not in "0123456789abcdefABCDEF" for digit in hex_digits):
                    raise VitrineError("Битая строка в inbox.json")
                chars.append(chr(int(hex_digits, 16)))
                index += 5
                continue
            raise VitrineError("Битая строка в inbox.json")
        chars.append(char)
        index += 1
    raise VitrineError("Оборванная строка в inbox.json")


def _matching_bracket(text: str, start: int) -> int:
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    if opening not in "{[":
        raise VitrineError("Ожидался список или объект в inbox.json")
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            _, index = _scan_string(text, index)
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise VitrineError("Оборванный JSON в inbox.json")


def _skip_value(text: str, index: int) -> int:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise VitrineError("Оборванное значение в inbox.json")
    char = text[index]
    if char == '"':
        _, end = _scan_string(text, index)
        return end
    if char in "{[":
        return _matching_bracket(text, index) + 1
    if char in "-0123456789":
        end = index + 1
        while end < len(text) and text[end] in "0123456789eE+-.":
            end += 1
        return end
    for literal in ("true", "false", "null"):
        if text.startswith(literal, index):
            return index + len(literal)
    raise VitrineError("Непонятное значение в inbox.json")


def _is_prefix(path: tuple[str, ...], target: tuple[str, ...]) -> bool:
    return len(path) < len(target) and target[: len(path)] == path


def _walk(text: str, index: int, path: tuple[str, ...], target: tuple[str, ...], found: list[tuple[int, int]]) -> int:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise VitrineError("Оборванный inbox.json")
    if path == target:
        if text[index] != "[":
            raise VitrineError("Список карточек в inbox.json не найден")
        end = _matching_bracket(text, index)
        found.append((index, end))
        return end + 1
    if not _is_prefix(path, target):
        return _skip_value(text, index)
    if text[index] == "{":
        return _walk_object(text, index, path, target, found)
    if text[index] == "[":
        return _walk_array(text, index, path, target, found)
    return _skip_value(text, index)


def _walk_object(
    text: str,
    index: int,
    path: tuple[str, ...],
    target: tuple[str, ...],
    found: list[tuple[int, int]],
) -> int:
    index += 1
    seen = False
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "}":
            return index + 1
        if seen:
            if index >= len(text) or text[index] != ",":
                raise VitrineError("В объекте inbox.json нет запятой")
            index = _skip_ws(text, index + 1)
        key, index = _scan_string(text, index)
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise VitrineError("В объекте inbox.json нет двоеточия")
        child = path + (key,)
        if child == target or _is_prefix(child, target):
            index = _walk(text, index + 1, child, target, found)
        else:
            index = _skip_value(text, index + 1)
        seen = True


def _walk_array(
    text: str,
    index: int,
    path: tuple[str, ...],
    target: tuple[str, ...],
    found: list[tuple[int, int]],
) -> int:
    index += 1
    position = 0
    seen = False
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "]":
            return index + 1
        if seen:
            if index >= len(text) or text[index] != ",":
                raise VitrineError("В списке inbox.json нет запятой")
            index = _skip_ws(text, index + 1)
        child = path + (str(position),)
        if child == target or _is_prefix(child, target):
            index = _walk(text, index, child, target, found)
        else:
            index = _skip_value(text, index)
        seen = True
        position += 1


def _array_span(text: str, path: tuple[str, ...]) -> tuple[int, int]:
    offset = 1 if text.startswith("\ufeff") else 0
    found: list[tuple[int, int]] = []
    _walk(text, offset, (), path, found)
    if len(found) != 1:
        raise VitrineError("Список карточек в inbox.json не найден")
    start, end = found[0]
    return start, end


def _detect_indent(text: str, start: int) -> str:
    newline = text.find("\n", start)
    if newline == -1 or newline > start + 80:
        return ""
    index = newline + 1
    indent = ""
    while index < len(text) and text[index] in " \t":
        indent += text[index]
        index += 1
    return indent


def _format_card(card: Mapping[str, str], indent: str) -> str:
    raw = json.dumps(card, ensure_ascii=False, indent=2)
    if not indent:
        return raw
    return "\n".join(indent + line if line else line for line in raw.split("\n"))


def _last_value_end(text: str, start: int, end: int) -> int:
    index = start + 1
    last: int | None = None
    seen = False
    while index <= end:
        index = _skip_ws(text, index)
        if index == end and text[index] == "]":
            if last is None:
                raise VitrineError("Пустой список обработан отдельно")
            return last
        if seen:
            if text[index] != ",":
                raise VitrineError("В списке карточек нет запятой")
            index = _skip_ws(text, index + 1)
        last = _skip_value(text, index)
        index = last
        seen = True
    raise VitrineError("Не удалось найти конец списка карточек")


def _value_at(data: object, path: tuple[str, ...]) -> object:
    node = data
    for part in path:
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise VitrineError("Путь к списку карточек оборвался")
    return node


def splice_card(text: str, card: Mapping[str, str]) -> str:
    if PLAN_PLUS_TAB != "План +":
        raise VitrineError("Вкладку «План +» переименовывать нельзя")
    data = json.loads(text)
    path = choose_array_path(data)
    start, end = _array_span(text, path)
    indent = _detect_indent(text, start)
    block = _format_card(card, indent)
    inner = text[start + 1 : end]
    if inner.strip() == "":
        newline = "\n" if "\n" in text else ""
        insertion = f"{newline}{block}{newline}" if newline or indent else block
        updated = text[: start + 1] + insertion + text[end:]
    else:
        last = _last_value_end(text, start, end)
        separator = ",\n" if "\n" in text[start:end] else ", "
        updated = text[:last] + separator + block + text[last:]
    if not is_single_insertion(text, updated):
        raise VitrineError("Правка затронула бы чужие карточки. Файл не меняю.")
    parsed = json.loads(updated)
    old_items = _value_at(data, path)
    new_items = _value_at(parsed, path)
    if not isinstance(old_items, list) or not isinstance(new_items, list):
        raise VitrineError("Список карточек пропал. Файл не меняю.")
    if old_items != new_items[:-1] or new_items[-1] != card:
        raise VitrineError("Старые карточки изменились бы. Файл не меняю.")
    return updated


def _require_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise VitrineError(f"Нет переменной {name}. Секрет в чат не присылайте.")
    return value


def validate_connection(env: Mapping[str, str]) -> dict[str, str]:
    host = _require_env(env, "VITRINE_HOST")
    user = _require_env(env, "VITRINE_USER")
    key = _require_env(env, "VITRINE_SSH_KEY")
    root = env.get("VITRINE_PATH", "").strip() or DEFAULT_REMOTE_ROOT
    if not _HOST_RE.fullmatch(host):
        raise VitrineError("VITRINE_HOST должен быть именем хоста. Адрес в код не пишите.")
    if not _USER_RE.fullmatch(user):
        raise VitrineError("VITRINE_USER содержит недопустимые символы.")
    if not _ROOT_RE.fullmatch(root) or ".." in root.split("/"):
        raise VitrineError("VITRINE_PATH должен быть путём к каталогу ~/Vplanist без ..")
    return {"host": host, "user": user, "key": key, "root": root}


def _open_key_file(key_value: str) -> tuple[Path, bool]:
    if "PRIVATE KEY" in key_value:
        handle = tempfile.NamedTemporaryFile(prefix="vitrine-key-", delete=False)
        try:
            handle.write(key_value.encode("utf-8"))
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
        finally:
            handle.close()
        return Path(handle.name), True
    path = Path(key_value).expanduser()
    if not path.is_file():
        raise VitrineError("VITRINE_SSH_KEY не является ключом и не указывает на файл ключа.")
    return path, False


def _remote_command(root: str) -> str:
    encoded = base64.b64encode(REMOTE_HELPER.encode("utf-8")).decode("ascii")
    code = f"import base64; exec(base64.b64decode('{encoded}'))"
    return f"VITRINE_REMOTE_ROOT={shlex.quote(root)} python3 -s -c {shlex.quote(code)}"


def ssh_argv(env: Mapping[str, str], key_path: Path) -> list[str]:
    settings = validate_connection(env)
    return [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        f"{settings['user']}@{settings['host']}",
        _remote_command(settings["root"]),
    ]


def _run_helper(root: Path, request: dict[str, object]) -> dict[str, object]:
    env = os.environ.copy()
    env["VITRINE_REMOTE_ROOT"] = str(root)
    encoded = base64.b64encode(REMOTE_HELPER.encode("utf-8")).decode("ascii")
    completed = subprocess.run(
        [sys.executable, "-s", "-c", f"import base64; exec(base64.b64decode('{encoded}'))"],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise VitrineError(detail or "Операция на витрине не выполнилась")
    payload = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(payload, dict):
        raise VitrineError("Витрина вернула не объект")
    return payload


def _ssh_request(env: Mapping[str, str], request: dict[str, object]) -> dict[str, object]:
    settings = validate_connection(env)
    key_path, delete_key = _open_key_file(settings["key"])
    try:
        completed = subprocess.run(
            ssh_argv(env, key_path),
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
    finally:
        if delete_key:
            key_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise VitrineError(detail or "SSH к витрине не выполнился")
    payload = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(payload, dict):
        raise VitrineError("Витрина вернула не объект")
    return payload


def _load_remote_texts(runner) -> tuple[str, str, str]:
    response = runner({"op": "read_many", "paths": [INBOX_PATH, *PUBLISHED_PATHS]})
    files = response.get("files")
    if not isinstance(files, dict):
        raise VitrineError("Не удалось прочитать файлы витрины")
    inbox = files.get(INBOX_PATH)
    if (
        not isinstance(inbox, dict)
        or not inbox.get("ok")
        or not isinstance(inbox.get("text"), str)
        or not isinstance(inbox.get("sha256"), str)
    ):
        raise VitrineError("На витрине нет data/inbox.json. Файл не создаю.")
    published_text = ""
    for path in PUBLISHED_PATHS:
        item = files.get(path)
        if isinstance(item, dict) and item.get("ok") and isinstance(item.get("text"), str):
            published_text = item["text"]
            break
    if not published_text:
        raise VitrineError("На витрине нет published.json. Карточку не добавляю.")
    return inbox["text"], published_text, inbox["sha256"]


def publish_card(
    raw: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    added_at: str | None = None,
    dry_run: bool = False,
    runner=None,
) -> dict[str, object]:
    """Add one card to the remote inbox. `runner` replaces SSH in tests."""
    stamp = added_at or now_moscow()
    card = card_from_mapping(raw, added_at=stamp)
    source = env if env is not None else os.environ

    def remote(request: dict[str, object]) -> dict[str, object]:
        return _ssh_request(source, request)

    if runner is None:
        validate_connection(source)
        runner = remote
    inbox_text, published_text, inbox_sha = _load_remote_texts(runner)
    inbox_data = json.loads(inbox_text)
    published_data = json.loads(published_text)
    duplicate = find_duplicate(card, inbox_data, published_data)
    if duplicate:
        raise VitrineError(duplicate + ". Карточку не добавляю.")
    updated = splice_card(inbox_text, card)
    path = choose_array_path(json.loads(updated))
    count = len(_value_at(json.loads(updated), path))
    if dry_run:
        return {"card": card, "count": count, "committed": False, "backup": ""}
    backup = runner({"op": "backup"})
    replaced = runner({"op": "replace_if_match", "sha256": inbox_sha, "text": updated})
    if not isinstance(replaced, dict) or not replaced.get("ok"):
        raise VitrineError("Витрина не подтвердила запись карточки")
    message = "Add Plan+ topic: " + " ".join(card["title"].split())
    if len(message) > 180:
        message = message[:177] + "..."
    committed = runner({"op": "commit", "message": message})
    revision = committed.get("revision", "") if isinstance(committed, dict) else ""
    backup_path = backup.get("backup", "") if isinstance(backup, dict) else ""
    return {
        "card": card,
        "count": count,
        "committed": True,
        "revision": revision if isinstance(revision, str) else "",
        "backup": backup_path if isinstance(backup_path, str) else "",
    }


REMOTE_HELPER = r"""
import datetime, hashlib, json, os, pathlib, subprocess, sys

INBOX = "data/inbox.json"
READABLE = {INBOX, "data/published.json", "published.json"}

def fail(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)

def root():
    raw = os.environ.get("VITRINE_REMOTE_ROOT", "")
    if not raw or ".." in raw.split("/"):
        fail("vitrine path is not allowed")
    path = pathlib.Path(raw).expanduser().resolve()
    if not path.is_dir():
        fail("vitrine project directory is missing")
    return path

def is_single_insertion(old, new):
    if len(new) <= len(old):
        return False
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    rest = old[prefix:]
    if rest == "":
        return True
    return new.endswith(rest) and len(new) - len(rest) >= prefix

def main():
    request = json.loads(sys.stdin.read())
    op = request.get("op")
    base = root()
    if op == "read_many":
        files = {}
        for rel in request.get("paths") or []:
            if rel not in READABLE:
                fail("read path is not allowed")
            target = base / rel
            if target.is_file():
                raw = target.read_bytes()
                files[rel] = {
                    "ok": True,
                    "text": raw.decode("utf-8"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            else:
                files[rel] = {"ok": False}
        json.dump({"files": files}, sys.stdout, ensure_ascii=False)
        return
    if op == "backup":
        target = base / INBOX
        if not target.is_file():
            fail("inbox is missing")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = base / "data" / ("inbox.json.bak-" + stamp)
        dest.write_bytes(target.read_bytes())
        json.dump({"ok": True, "backup": "data/" + dest.name}, sys.stdout)
        return
    if op == "replace_if_match":
        target = base / INBOX
        current = target.read_bytes()
        if hashlib.sha256(current).hexdigest() != request.get("sha256"):
            fail("inbox.json changed before write")
        old = current.decode("utf-8")
        new = request.get("text")
        if not isinstance(new, str) or not is_single_insertion(old, new):
            fail("refusing to rewrite inbox.json")
        json.loads(new)
        temporary = target.parent / (target.name + ".tmp")
        temporary.write_bytes(new.encode("utf-8"))
        os.replace(temporary, target)
        json.dump({"ok": True}, sys.stdout)
        return
    if op == "commit":
        message = request.get("message") or ""
        if not isinstance(message, str) or not message.strip() or "\n" in message:
            fail("commit message is empty")
        add = subprocess.run(["git", "-C", str(base), "add", "--", INBOX], capture_output=True, text=True)
        if add.returncode != 0:
            fail(add.stderr.strip() or "git add failed")
        commit = subprocess.run(
            ["git", "-C", str(base), "commit", "-m", message.strip(), "--", INBOX],
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            fail(commit.stderr.strip() or commit.stdout.strip() or "git commit failed")
        rev = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        json.dump({"ok": True, "revision": rev.stdout.strip()}, sys.stdout)
        return
    fail("unknown operation")

main()
"""
