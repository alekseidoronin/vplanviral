from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from viraldzen.vitrine import (
    PLAN_PLUS_TAB,
    VitrineError,
    _run_helper,
    build_card,
    card_from_mapping,
    find_duplicate,
    is_single_insertion,
    now_moscow,
    publish_card,
    splice_card,
    ssh_argv,
    validate_connection,
)


SAMPLE = {
    "title": "Звонят из Соцфонда и грозят отменить детское пособие. Что делать",
    "hook": "Просят перевести сбережения на транзитный счет.",
    "source_url": "https://ria.ru/20261012/moshenniki-2123852464.html",
    "source_date": "12.10.2026 01:32 МСК",
    "why": "Родителям понятна угроза потерять выплату.",
}
ADDED = "14.10.2026 17:55 МСК"


def card() -> dict[str, str]:
    return build_card(added_at=ADDED, **SAMPLE)


class CardTests(unittest.TestCase):
    def test_added_at_is_moscow_clock(self) -> None:
        moment = datetime(2026, 10, 14, 14, 5, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(now_moscow(moment), "14.10.2026 17:05 МСК")

    def test_tab_name_stays_plan_plus(self) -> None:
        self.assertEqual(PLAN_PLUS_TAB, "План +")

    def test_dispatcher_fields_are_rejected(self) -> None:
        raw = dict(SAMPLE)
        raw["queue"] = "4"
        with self.assertRaises(VitrineError):
            card_from_mapping(raw, added_at=ADDED)

    def test_card_has_source_date_and_added_at(self) -> None:
        built = card()
        self.assertEqual(built["source_date"], SAMPLE["source_date"])
        self.assertEqual(built["date"], ADDED)
        self.assertNotIn("queue", built)
        self.assertNotIn("article", built)


class SpliceTests(unittest.TestCase):
    def test_appends_to_top_level_array_without_touching_old_bytes(self) -> None:
        original = '[\n  {"title": "Старая", "source_url": "https://example.com/a"}\n]\n'
        updated = splice_card(original, card())
        self.assertTrue(updated.startswith(original[: original.index("}") + 1]))
        self.assertTrue(is_single_insertion(original, updated))
        parsed = json.loads(updated)
        self.assertEqual(parsed[0]["title"], "Старая")
        self.assertEqual(parsed[1]["title"], SAMPLE["title"])
        self.assertNotIn("queue", parsed[1])

    def test_appends_only_inside_plan_plus_tab(self) -> None:
        original = json.dumps(
            {
                "tabs": [
                    {"name": "План +", "items": [{"title": "Тест", "source_url": "https://example.com/t"}]},
                    {"name": "Статьи", "items": [{"title": "Уже вышло", "source_url": "https://example.com/p"}]},
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        updated = splice_card(original, card())
        self.assertTrue(is_single_insertion(original, updated))
        parsed = json.loads(updated)
        self.assertEqual(parsed["tabs"][1], json.loads(original)["tabs"][1])
        self.assertEqual(parsed["tabs"][0]["name"], "План +")
        self.assertEqual(len(parsed["tabs"][0]["items"]), 2)
        self.assertEqual(parsed["tabs"][0]["items"][0]["title"], "Тест")

    def test_ambiguous_lists_are_left_untouched(self) -> None:
        original = '{"one": [{"title": "А"}], "two": [{"title": "Б"}]}'
        with self.assertRaises(VitrineError):
            splice_card(original, card())

    def test_duplicate_in_queue_and_published(self) -> None:
        inbox = [{"title": SAMPLE["title"], "source_url": "https://example.com/other"}]
        published = {"articles": [{"title": "Другая", "url": SAMPLE["source_url"]}]}
        self.assertIn("очереди", find_duplicate(card(), inbox, {"articles": []}) or "")
        self.assertIn("опубликованных", find_duplicate(card(), [], published) or "")
        self.assertIsNone(find_duplicate(card(), [], {"articles": [{"title": "Иная"}]}))


class PublishTests(unittest.TestCase):
    def _repo(self, root: Path, inbox: object, published: object) -> None:
        (root / "data").mkdir()
        (root / "www").mkdir()
        (root / "data" / "inbox.json").write_text(
            json.dumps(inbox, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "data" / "published.json").write_text(
            json.dumps(published, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "www" / "index.html").write_text("<p>витрина</p>\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "--", "data/inbox.json", "data/published.json", "www/index.html"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)

    def test_publish_adds_one_card_and_commits_only_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(
                root,
                [{"title": "Тестовая карточка", "source_url": "https://example.com/old"}],
                {"articles": [{"title": "Опубликованная", "url": "https://example.com/live"}]},
            )
            html_before = (root / "www" / "index.html").read_bytes()
            published_before = (root / "data" / "published.json").read_bytes()
            calls: list[str] = []

            def runner(request: dict) -> dict:
                calls.append(str(request["op"]))
                return _run_helper(root, request)

            result = publish_card(SAMPLE, added_at=ADDED, runner=runner)
            self.assertEqual(calls, ["read_many", "backup", "replace_if_match", "commit"])
            self.assertTrue(result["committed"])
            inbox = json.loads((root / "data" / "inbox.json").read_text(encoding="utf-8"))
            self.assertEqual(len(inbox), 2)
            self.assertEqual(inbox[0]["title"], "Тестовая карточка")
            self.assertEqual(inbox[1]["source_url"], SAMPLE["source_url"])
            self.assertNotIn("queue", inbox[1])
            self.assertEqual((root / "data" / "published.json").read_bytes(), published_before)
            self.assertEqual((root / "www" / "index.html").read_bytes(), html_before)
            backups = list((root / "data").glob("inbox.json.bak-*"))
            self.assertEqual(len(backups), 1)
            shown = subprocess.run(
                ["git", "show", "--name-only", "--format=", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(shown.stdout.strip(), "data/inbox.json")

    def test_duplicate_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root, [{"title": SAMPLE["title"], "hook": "уже есть"}], {"articles": []})
            before = (root / "data" / "inbox.json").read_bytes()

            def runner(request: dict) -> dict:
                self.assertEqual(request["op"], "read_many")
                return _run_helper(root, request)

            with self.assertRaises(VitrineError) as caught:
                publish_card(SAMPLE, added_at=ADDED, runner=runner)
            self.assertIn("очереди", str(caught.exception))
            self.assertEqual((root / "data" / "inbox.json").read_bytes(), before)

    def test_rewrite_of_whole_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo(root, [{"title": "Старая"}], {"articles": []})
            before = (root / "data" / "inbox.json").read_bytes()
            _run_helper(root, {"op": "backup"})
            with self.assertRaises(VitrineError):
                _run_helper(
                    root,
                    {
                        "op": "replace_if_match",
                        "sha256": "0" * 64,
                        "text": "[]\n",
                    },
                )
            self.assertEqual((root / "data" / "inbox.json").read_bytes(), before)

    def test_env_names_only(self) -> None:
        with self.assertRaises(VitrineError) as caught:
            validate_connection({})
        self.assertIn("VITRINE_HOST", str(caught.exception))
        self.assertNotIn("BEGIN", str(caught.exception))
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("not-a-real-secret-file\n", encoding="utf-8")
            argv = ssh_argv(
                {
                    "VITRINE_HOST": "vitrine.example",
                    "VITRINE_USER": "editor",
                    "VITRINE_SSH_KEY": str(key),
                },
                key,
            )
            joined = " ".join(argv)
            self.assertIn("editor@vitrine.example", argv)
            self.assertNotIn("@", joined.split("editor@vitrine.example", 1)[0])
            self.assertIn("~/Vplanist", joined)
            self.assertNotIn("BEGIN OPENSSH", joined)


class EnvLeakTests(unittest.TestCase):
    def test_missing_env_does_not_echo_key_material(self) -> None:
        previous = os.environ.get("VITRINE_SSH_KEY")
        os.environ["VITRINE_SSH_KEY"] = "SUPER-SECRET-KEY-VALUE"
        try:
            with self.assertRaises(VitrineError) as caught:
                validate_connection(os.environ)
            self.assertNotIn("SUPER-SECRET-KEY-VALUE", str(caught.exception))
        finally:
            if previous is None:
                os.environ.pop("VITRINE_SSH_KEY", None)
            else:
                os.environ["VITRINE_SSH_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
