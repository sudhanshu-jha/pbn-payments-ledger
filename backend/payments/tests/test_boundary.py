"""The payments app must treat mock_processor as an external system."""

import ast
import pathlib

from django.test import SimpleTestCase


PAYMENTS_DIR = pathlib.Path(__file__).resolve().parent.parent
ALLOWED_IMPORTER = PAYMENTS_DIR / "gateway.py"
ALLOWED_MODULE = "mock_processor.public"


def processor_imports(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mock_processor"):
                    yield alias.name
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mock_processor"):
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


class ProcessorBoundaryTests(SimpleTestCase):
    def test_only_the_gateway_imports_the_processor_and_only_its_public_facade(self):
        offenders = {}
        for path in PAYMENTS_DIR.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            found = list(processor_imports(path))
            if not found:
                continue
            if path != ALLOWED_IMPORTER or any(
                not name.startswith(ALLOWED_MODULE) for name in found
            ):
                offenders[str(path.relative_to(PAYMENTS_DIR))] = found
        self.assertEqual(
            offenders, {}, f"payments must only import {ALLOWED_MODULE} from gateway.py"
        )

    def test_gateway_imports_the_facade(self):
        self.assertEqual(list(processor_imports(ALLOWED_IMPORTER)), ["mock_processor.public"])

    def test_payments_models_have_no_relation_to_processor_tables(self):
        from django.apps import apps

        for model in apps.get_app_config("payments").get_models():
            for field in model._meta.get_fields():
                related = getattr(field, "related_model", None)
                if related is not None:
                    self.assertNotEqual(related._meta.app_label, "mock_processor", field)
