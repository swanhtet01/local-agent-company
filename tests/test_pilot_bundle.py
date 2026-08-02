import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.build_pilot_bundle import (
    BundleBuildError, _personal_path_markers, _validated_source_payload,
    build_pilot_bundle,
)
from scripts.verify_pilot_bundle import (
    BUNDLE_SCHEMA, BundleVerificationError, MANIFEST_NAME, verify_pilot_bundle,
)


class PilotBundleTests(unittest.TestCase):
    def test_personal_absolute_paths_are_rejected_before_packaging(self) -> None:
        marker = b"private" + b"-home-root"
        payload = b"root = '" + marker + b"/project'"
        with patch(
            "scripts.build_pilot_bundle._personal_path_markers",
            return_value=(marker,),
        ):
            with self.assertRaisesRegex(
                BundleBuildError, "bundle_source_contains_personal_path",
            ):
                _validated_source_payload("example.py", payload)

    def test_bundle_is_deterministic_sanitized_and_tamper_evident(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            first = build_pilot_bundle(root, output)
            second = build_pilot_bundle(root, output)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["bundleId"], second["bundleId"])
            self.assertEqual(first["bundleSha256"], second["bundleSha256"])
            self.assertFalse(first["privateStateIncluded"])
            self.assertFalse(first["credentialsIncluded"])
            self.assertFalse(first["modelWeightsIncluded"])
            self.assertFalse(first["externalPublicationAuthorized"])

            archive = Path(first["bundlePath"])
            verified = verify_pilot_bundle(archive)
            self.assertEqual(verified["status"], "verified")
            self.assertFalse(verified["archiveExtracted"])
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                manifest = json.loads(bundle.read(MANIFEST_NAME))
                bundled_payload = b"\n".join(
                    bundle.read(name).lower()
                    for name in names
                    if name != MANIFEST_NAME
                )
            self.assertEqual(manifest["schema"], BUNDLE_SCHEMA)
            self.assertEqual(manifest["licenseStatus"], "no_license_grant")
            self.assertIn("BUNDLE-README.txt", names)
            self.assertIn("local-ai.cmd", names)
            self.assertIn("scripts/verify_pilot_bundle.py", names)
            self.assertTrue(any(name.startswith("src/local_company/") for name in names))
            forbidden = (".git/", ".env", "company.db", "outputs/", "validation-packs/")
            self.assertFalse(any(any(token in name for token in forbidden) for name in names))
            for marker in _personal_path_markers():
                self.assertNotIn(marker, bundled_payload)

            tampered = output / "tampered.zip"
            shutil.copyfile(archive, tampered)
            payload = bytearray(tampered.read_bytes())
            payload[len(payload) // 2] ^= 0x01
            tampered.write_bytes(payload)
            with self.assertRaises(BundleVerificationError):
                verify_pilot_bundle(tampered)

            forged = output / "forged-id.zip"
            with zipfile.ZipFile(archive, "r") as original, zipfile.ZipFile(
                forged, "w", compression=zipfile.ZIP_DEFLATED,
            ) as changed:
                for info in original.infolist():
                    payload = original.read(info.filename)
                    if info.filename == MANIFEST_NAME:
                        value = json.loads(payload)
                        value["bundleId"] = "0" * 12
                        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("ascii")
                    changed.writestr(info, payload)
            with self.assertRaisesRegex(BundleVerificationError, "bundle_identity_invalid"):
                verify_pilot_bundle(forged)


if __name__ == "__main__":
    unittest.main()
