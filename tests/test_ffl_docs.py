import tempfile
import unittest
from pathlib import Path

from picklist.domain import ffl_docs

EZ_CHECK_TEXT = """ATF FFL eZ Check
License Number:1-74-XXX-XX-XX-12345
Expiration Date:10/01/2029
License Name: WILD WEST GUNS & AMMO LLC
Trade Name: WILD WEST GUNS, Gun Vault
Premise Address: 490 IH 35 South
San Marcos, TX 78666
Mailing Address: PO Box 12
"""

MASTER_TEXT = "Something else\nJUNEAU OUTFITTERS INC:123 MAIN ST JUNEAU AL:36480:1-63-001-01-9D-12345:\n"


def ship_to(**overrides):
    base = {"name": "Wild West Guns", "addr_1": "490 I-35", "addr_2": None, "addr_3": None,
            "city": "San Marcos", "state": "TX", "zip": "78666"}
    base.update(overrides)
    return base


class PathTests(unittest.TestCase):
    def test_parse_path_map_and_map_path(self):
        pairs = ffl_docs.parse_path_map(r"V:\=/mnt/carms;\\2CARMS\CARMS$=/mnt/carms")
        self.assertEqual(pairs, [("V:\\", "/mnt/carms"), ("\\\\2CARMS\\CARMS$", "/mnt/carms")])
        self.assertEqual(ffl_docs.map_path(r"V:\Documents\FFLs\A.pdf", pairs), "/mnt/carms/Documents/FFLs/A.pdf")
        self.assertEqual(ffl_docs.map_path(r"\\2CARMS\CARMS$\FFLs\B.pdf", pairs), "/mnt/carms/FFLs/B.pdf")
        self.assertEqual(ffl_docs.map_path(r"C:\other\x.pdf", pairs), r"C:\other\x.pdf")

    def test_join_doc_path(self):
        self.assertEqual(ffl_docs.join_doc_path(r"V:\Docs\5.13.2026", "LIPSEYS FFL.pdf"), r"V:\Docs\5.13.2026\LIPSEYS FFL.pdf")
        self.assertEqual(ffl_docs.join_doc_path("/mnt/carms/docs/", "a.pdf"), "/mnt/carms/docs/a.pdf")
        self.assertEqual(ffl_docs.join_doc_path("", "a.pdf"), "a.pdf")

    def test_resolve_document_path_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "FFLs").mkdir()
            target = root / "FFLs" / "DEALER FFL.pdf"
            target.write_bytes(b"%PDF-1.4 fake")
            pairs = [("V:\\", str(root))]
            path, status, _ = ffl_docs.resolve_document_path(r"V:\FFLs", "DEALER FFL.pdf", path_map=pairs, roots=[root])
            self.assertEqual(status, "ok")
            self.assertEqual(path, target.resolve())
            self.assertEqual(ffl_docs.resolve_document_path(r"V:\FFLs", "missing.pdf", path_map=pairs, roots=[root])[1], "not_found")
            self.assertEqual(ffl_docs.resolve_document_path(r"V:\FFLs", "notes.txt", path_map=pairs, roots=[root])[1], "unsupported")
            self.assertEqual(ffl_docs.resolve_document_path(r"V:\FFLs", "DEALER FFL.pdf", path_map=pairs, roots=[])[1], "no_roots")
            outside = Path(tempfile.gettempdir()).resolve()
            other_root = root / "FFLs"
            self.assertEqual(
                ffl_docs.resolve_document_path(str(outside), "x.pdf", path_map=[], roots=[other_root])[1],
                "out_of_roots",
            )
            self.assertEqual(ffl_docs.resolve_document_path("", "", path_map=pairs, roots=[root])[1], "missing_path")
            key = ffl_docs.doc_key("DEALER FFL.pdf", target)
            self.assertTrue(key.startswith("DEALER FFL.pdf|"))
            self.assertEqual(key.count("|"), 2)


class ClassifyAndParseTests(unittest.TestCase):
    def test_classify_kind(self):
        self.assertEqual(ffl_docs.classify_kind("FFL EZ CHECK 2026.pdf"), "ffl_ez_check")
        self.assertEqual(ffl_docs.classify_kind("LIPSEYS FFL DEC 2027.pdf"), "ffl_master")
        self.assertEqual(ffl_docs.classify_kind("scan.pdf", r"V:\Documents\FFLs\2026"), "ffl_master")
        self.assertEqual(ffl_docs.classify_kind("PO 12345.pdf"), "other")
        self.assertEqual(ffl_docs.classify_kind("FFL notes.txt"), "other")

    def test_parse_ez_check_labels(self):
        parsed = ffl_docs.parse_ffl_doc(EZ_CHECK_TEXT)
        self.assertEqual(parsed["legal_names"], ["WILD WEST GUNS & AMMO LLC"])
        self.assertEqual(parsed["trade_names"], ["WILD WEST GUNS", "Gun Vault"])
        self.assertEqual(parsed["premise"], "490 IH 35 South San Marcos, TX 78666")
        self.assertEqual(parsed["license_number"], "1-74-XXX-XX-XX-12345")
        self.assertEqual(parsed["expiration"], "10/01/2029")

    def test_parse_master_footer(self):
        parsed = ffl_docs.parse_ffl_doc(MASTER_TEXT)
        self.assertEqual(parsed["legal_names"], ["JUNEAU OUTFITTERS INC"])
        self.assertEqual(parsed["premise"], "123 MAIN ST JUNEAU AL 36480")
        self.assertEqual(parsed["license_number"], "1-63-001-01-9D-12345")
        self.assertEqual(ffl_docs.parse_ffl_doc("")["legal_names"], [])

    def test_merge_parsed_prefers_first_premise(self):
        merged = ffl_docs.merge_parsed([
            {"legal_names": ["A"], "trade_names": [], "premise": None},
            {"legal_names": ["A", "B"], "trade_names": ["T"], "premise": "1 Main St 11111"},
        ])
        self.assertEqual(merged["legal_names"], ["A", "B"])
        self.assertEqual(merged["premise"], "1 Main St 11111")


class CompareTests(unittest.TestCase):
    def test_same_premise_written_two_ways_passes(self):
        parsed = ffl_docs.parse_ffl_doc(EZ_CHECK_TEXT)
        findings = {f["reason_code"]: f for f in ffl_docs.compare(ship_to(), parsed)}
        self.assertTrue(findings[ffl_docs.REASON_NAME]["passed"])
        self.assertTrue(findings[ffl_docs.REASON_PREMISE]["passed"], findings[ffl_docs.REASON_PREMISE])
        self.assertIn("490 I-35 San Marcos TX 78666", findings[ffl_docs.REASON_PREMISE]["detail"]["shipto_addr"])

    def test_zip_mismatch_fails_with_both_strings(self):
        parsed = ffl_docs.parse_ffl_doc(MASTER_TEXT)
        findings = {f["reason_code"]: f for f in ffl_docs.compare(
            ship_to(name="Juneau Outfitters", addr_1="123 Main St", city="Juneau", state="AK", zip="99801"), parsed)}
        self.assertTrue(findings[ffl_docs.REASON_NAME]["passed"])
        premise = findings[ffl_docs.REASON_PREMISE]
        self.assertFalse(premise["passed"])
        self.assertIn("ZIP 99801", premise["detail"]["why"])
        self.assertEqual(premise["detail"]["ffl_premise"], "123 MAIN ST JUNEAU AL 36480")

    def test_name_mismatch_fails(self):
        parsed = ffl_docs.parse_ffl_doc(EZ_CHECK_TEXT)
        findings = {f["reason_code"]: f for f in ffl_docs.compare(ship_to(name="Bob's Bait Shop"), parsed)}
        self.assertFalse(findings[ffl_docs.REASON_NAME]["passed"])
        self.assertEqual(findings[ffl_docs.REASON_NAME]["detail"]["ffl_legal_name"], "WILD WEST GUNS & AMMO LLC")

    def test_unparsed_document_passes(self):
        findings = ffl_docs.compare(ship_to(), {"legal_names": [], "trade_names": [], "premise": None})
        self.assertTrue(all(f["passed"] for f in findings))
        self.assertTrue(all(f["detail"].get("unparsed") for f in findings))

    def test_stale_ship_to_record_is_flagged(self):
        parsed = ffl_docs.parse_ffl_doc(EZ_CHECK_TEXT)
        findings = {f["reason_code"]: f for f in ffl_docs.compare(
            ship_to(ffl_number="1-74-012-07-6K-12345", ffl_expiry_raw="10/1/2026"), parsed)}
        record = findings[ffl_docs.REASON_RECORD]
        self.assertFalse(record["passed"])
        self.assertIn("10/1/2026 on the ship-to vs 10/01/2029", record["detail"]["why"])
        ok = {f["reason_code"]: f for f in ffl_docs.compare(
            ship_to(ffl_number="1-74-012-07-6K-12345", ffl_expiry_raw="10/01/2029"), parsed)}
        self.assertTrue(ok[ffl_docs.REASON_RECORD]["passed"])
        wrong_number = {f["reason_code"]: f for f in ffl_docs.compare(
            ship_to(ffl_number="1-74-012-07-6K-99999", ffl_expiry_raw="10/01/2029"), parsed)}
        self.assertIn("number", wrong_number[ffl_docs.REASON_RECORD]["detail"]["why"])

    def test_different_street_same_zip_fails(self):
        parsed = {"legal_names": ["WILD WEST GUNS"], "trade_names": [], "premise": "900 Oak Avenue San Marcos TX 78666"}
        findings = {f["reason_code"]: f for f in ffl_docs.compare(ship_to(), parsed)}
        self.assertFalse(findings[ffl_docs.REASON_PREMISE]["passed"])


class ExtractTests(unittest.TestCase):
    def test_unsupported_and_missing_ocr_never_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "a.txt"
            txt.write_text("x")
            self.assertEqual(ffl_docs.extract_text(txt)[1], "none")
            img = Path(tmp) / "scan.png"
            img.write_bytes(b"not an image")
            text, method, error = ffl_docs.extract_text(img, ocr_enabled=False)
            self.assertEqual((text, method), ("", "none"))
            self.assertIn("OCR", error)
            text, method, error = ffl_docs.extract_text(img, ocr_enabled=True)
            self.assertEqual(text, "")
            self.assertIsNotNone(error)
            bad_pdf = Path(tmp) / "bad.pdf"
            bad_pdf.write_bytes(b"not a pdf")
            text, method, error = ffl_docs.extract_text(bad_pdf, ocr_enabled=False)
            self.assertEqual(text, "")
            self.assertIsNotNone(error)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "FFLs").mkdir()
        (self.root / "FFLs" / "WILD WEST FFL EZ CHECK.pdf").write_bytes(b"%PDF-1.4")
        (self.root / "FFLs" / "JUNEAU FFL.pdf").write_bytes(b"%PDF-1.4")
        ffl_docs.configure(path_map=[("V:\\", str(self.root))], roots=[self.root], max_docs_per_run=20, ocr_enabled=False)
        self.cache: dict[str, dict] = {}
        self.extracted: list[str] = []
        self._orig_extract = ffl_docs.extract_text

        def fake_extract(path, **_):
            self.extracted.append(path.name)
            return (EZ_CHECK_TEXT if "WILD" in path.name else MASTER_TEXT), "pypdf", None

        ffl_docs.extract_text = fake_extract

    def tearDown(self):
        ffl_docs.extract_text = self._orig_extract
        ffl_docs.configure(path_map=[], roots=[], max_docs_per_run=ffl_docs.DEFAULT_MAX_DOCS_PER_RUN, ocr_enabled=False)
        self.tmp.cleanup()

    def _cache_get(self, key):
        return self.cache.get(key)

    def _cache_put(self, key, **row):
        self.cache[key] = row

    def _order(self, order_id, name, zip_code="78666", city="San Marcos", state="TX", addr="490 I-35", **extra):
        base = {
            "order_id": order_id, "firearms": True, "closed": False, "due": "2026-10-05",
            "docs": {"ffl_ez_check": 1, "ffl_master": 0, "attachments": 2},
            "flags": {}, "holds": [],
            "ship_to": {"name": name, "addr_1": addr, "city": city, "state": state, "zip": zip_code, "ffl_number": "1-74-012-07-6K-12345", "ffl_expiry_raw": "10/01/2029"},
        }
        base.update(extra)
        return base

    def test_findings_cache_and_candidates(self):
        docs = {
            "SO-1": [{"DOCUMENT_ID": "WILD WEST FFL EZ CHECK.pdf", "DOC_FILE_PATH": r"V:\FFLs"}, {"DOCUMENT_ID": "PO 1.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
            "SO-2": [{"DOCUMENT_ID": "JUNEAU FFL.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
            "SO-3": [{"DOCUMENT_ID": "JUNEAU FFL.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
            "SO-4": [{"DOCUMENT_ID": "gone.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
        }
        orders = [
            self._order("SO-1", "Wild West Guns"),
            self._order("SO-2", "Juneau Outfitters", zip_code="99801", city="Juneau", state="AK", addr="123 Main St"),
            self._order("SO-3", "Juneau Outfitters", holds=[{"reason_code": "credit_status_hold", "blocking": True}]),
            self._order("SO-4", "Nobody"),
            self._order("SO-5", "No docs", docs={"ffl_ez_check": 0, "ffl_master": 0}),
            self._order("SO-6", "Not firearms", firearms=False),
        ]
        result = ffl_docs.findings_for_orders(
            orders, fetch_documents=lambda so: docs.get(so, []), cache_get=self._cache_get, cache_put=self._cache_put,
        )
        self.assertEqual(sorted(result), ["SO-1", "SO-2"])
        self.assertTrue(all(f["passed"] for f in result["SO-1"]))
        premise = next(f for f in result["SO-2"] if f["reason_code"] == ffl_docs.REASON_PREMISE)
        self.assertFalse(premise["passed"])
        self.assertEqual(self.extracted, ["WILD WEST FFL EZ CHECK.pdf", "JUNEAU FFL.pdf"])
        self.assertEqual(len(self.cache), 2)
        self.assertEqual(next(iter(self.cache.values()))["method"], "pypdf")

        # Second run: everything comes from cache, nothing is re-extracted.
        self.extracted.clear()
        self.cache = {k: {**v, "parsed": v["parsed"]} for k, v in self.cache.items()}
        again = ffl_docs.findings_for_orders(
            orders, fetch_documents=lambda so: docs.get(so, []), cache_get=self._cache_get, cache_put=self._cache_put,
        )
        self.assertEqual(sorted(again), ["SO-1", "SO-2"])
        self.assertEqual(self.extracted, [])

    def test_budget_caps_new_reads(self):
        docs = {
            "SO-1": [{"DOCUMENT_ID": "WILD WEST FFL EZ CHECK.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
            "SO-2": [{"DOCUMENT_ID": "JUNEAU FFL.pdf", "DOC_FILE_PATH": r"V:\FFLs"}],
        }
        orders = [self._order("SO-2", "Juneau Outfitters", due="2026-10-09"), self._order("SO-1", "Wild West Guns", due="2026-10-02")]
        result = ffl_docs.findings_for_orders(
            orders, fetch_documents=lambda so: docs[so], cache_get=self._cache_get, cache_put=self._cache_put, max_docs=1,
        )
        self.assertEqual(list(result), ["SO-1"])  # earliest due first
        self.assertEqual(self.extracted, ["WILD WEST FFL EZ CHECK.pdf"])

    def test_fetch_failure_is_skipped(self):
        def boom(_so):
            raise RuntimeError("erp down")

        result = ffl_docs.findings_for_orders(
            [self._order("SO-1", "Wild West Guns")], fetch_documents=boom, cache_get=self._cache_get, cache_put=self._cache_put,
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
