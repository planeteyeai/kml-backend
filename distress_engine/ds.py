import importlib.util
import os


def _load_source_module():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_source_path = os.path.join(base_dir, "distress_source.py")
    if os.path.isfile(bundled_source_path):
        source_path = bundled_source_path
    else:
        source_path = os.getenv(
            "DISTRESS_DS_SOURCE",
            os.path.normpath(os.path.join(base_dir, "..", "distressanalyzerv2.0", "ds.py")),
        )
    if not os.path.isfile(source_path):
        raise FileNotFoundError(
            f"Distress logic source not found at: {source_path}. "
            "Set DISTRESS_DS_SOURCE to a valid ds.py path."
        )
    spec = importlib.util.spec_from_file_location("distress_source_ds", source_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load ds.py module from: {source_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_source = _load_source_module()
process_rotated_image_job = _source.process_rotated_image_job
export_expanded_excel_for_image_job = _source.export_expanded_excel_for_image_job
