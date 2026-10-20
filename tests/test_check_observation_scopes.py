"""The scope check flags observations that recall could not attribute to one source."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_observation_scopes.py"
spec = importlib.util.spec_from_file_location("check_observation_scopes", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def fact(memory_id, document_id, version="1"):
    return {"id": memory_id, "document_id": document_id, "metadata": {"source_version": version}}


def observation(memory_id, *parents):
    return {"id": memory_id, "source_memory_ids": list(parents)}


class ObservationScopeCheckTests(unittest.TestCase):
    def merged(self, memories, observations):
        items = [{"id": key, "fact_type": "observation"} for key in observations]

        def call(base, path, method="GET"):
            return memories[path.rsplit("/", 1)[1]]

        with patch.object(check, "call", call):
            return check.merged_observations("http://hindsight", items)

    def test_observation_from_one_source_is_not_flagged_even_through_a_chain(self):
        memories = {
            "f1": fact("f1", "src_a"), "f2": fact("f2", "src_a"),
            "o1": observation("o1", "f1", "f2"),
            "o2": observation("o2", "o1"),
        }
        self.assertEqual(self.merged(memories, ["o1", "o2"]), [])

    def test_observation_merging_two_sources_is_flagged(self):
        memories = {
            "f1": fact("f1", "src_a"), "f2": fact("f2", "src_b"),
            "o1": observation("o1", "f1", "f2"),
        }
        flagged = self.merged(memories, ["o1"])
        self.assertEqual([(item["id"], sorted(sources)) for item, sources in flagged],
                         [("o1", [("src_a", "1"), ("src_b", "1")])])

    def test_two_versions_of_one_source_are_two_sources_for_recall(self):
        memories = {
            "f1": fact("f1", "src_a", "1"), "f2": fact("f2", "src_a", "2"),
            "o1": observation("o1", "f1", "f2"),
        }
        self.assertEqual(len(self.merged(memories, ["o1"])), 1)

    def test_document_chunk_ids_resolve_to_their_source(self):
        memories = {
            "f1": fact("f1", "doc:doc_x:v1:e1:p0-203"), "f2": fact("f2", "doc:doc_x:v1:e1:p0-203"),
            "o1": observation("o1", "f1", "f2"),
        }
        self.assertEqual(self.merged(memories, ["o1"]), [])

    def test_observation_without_any_source_is_flagged(self):
        self.assertEqual(len(self.merged({"o1": observation("o1")}, ["o1"])), 1)


if __name__ == "__main__":
    unittest.main()
