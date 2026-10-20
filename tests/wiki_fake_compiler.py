"""A deterministic stand-in for the wiki model, to test the whole compile pipeline.

Each evidence window yields one claim per sentence of its first segment, quoting
that sentence. The summary rests on the first claim. Routing sends each claim to
the topic or entity one of its keywords names, reusing an existing page with that
title, and reports a contradiction when a claim and a listed existing claim say
different things about the same subject ("X is Y", "X — це Y").
"""
import json
import re

_SENTENCE = re.compile(r"[^.!?]+[.!?]")
_FACT = re.compile(r"^\s*(.+?)\s+(?:is|are|—\s*це|—)\s+(.+?)[.!?]?\s*$", re.IGNORECASE)


class FakeCompiler:
    def __init__(self, topics):
        # keyword -> (kind, title)
        self.topics = topics
        self.queries = {"window": [], "summary": [], "routing": []}

    async def __call__(self, query, **kwargs):
        if "UNTRUSTED_DATA=" not in query:
            # Not a wiki request (a category suggestion, for example): an unusable answer.
            return {"text": "{}"}
        data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
        if "evidence" in data:
            self.queries["window"].append(query)
            answer = self.window(data)
        elif "pages" in data:
            self.queries["routing"].append(query)
            answer = self.routes(data)
        else:
            self.queries["summary"].append(query)
            first = data["claims"][0]
            answer = {"summary": [{"text": "In short: " + first["text"][:200].strip(), "claims": [first["n"]]}]}
        return {"text": json.dumps(answer, ensure_ascii=False)}

    @staticmethod
    def window(data):
        example = data["example_patch"]
        citation = example["claims"][0]["citations"][0]
        segment = data["evidence"]["segments"][0]
        claims = []
        for sentence in _SENTENCE.findall(segment["text"]):
            quote = sentence.strip()
            claims.append({"text": quote, "citations": [{**citation, "passage_start": segment["passage_index"],
                                                         "passage_end": segment["passage_index"] + 1, "quote": quote}]})
        return {**example, "claims": claims or example["claims"]}

    def routes(self, data):
        existing = {page["title"].casefold(): page["page_id"] for page in data["pages"]}
        routes = {}
        for claim in data["claims"]:
            text = claim["text"].casefold()
            for keyword, (kind, title) in self.topics.items():
                if keyword in text:
                    key = existing.get(title.casefold()) or (kind, title)
                    routes.setdefault(key, {"claims": [], "contradicts": []})["claims"].append(claim["n"])
        result = []
        for key, route in routes.items():
            if isinstance(key, str):
                for claim in data["claims"]:
                    for shown in data["existing_claims"].get(key, []):
                        if claim["n"] in route["claims"] and self.contradicts(claim["text"], shown["text"]):
                            route["contradicts"].append({"claim": claim["n"], "page_id": key, "existing_claim": shown["n"]})
            result.append({"page": key if isinstance(key, str) else {"kind": key[0], "title": key[1]}, **route})
        return {"routes": result[:6]}

    @staticmethod
    def contradicts(left, right):
        first, second = _FACT.match(left), _FACT.match(right)
        return bool(first and second) and first.group(1).casefold() == second.group(1).casefold() \
            and first.group(2).casefold() != second.group(2).casefold()
