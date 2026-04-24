import json
import sys

from ds import process_rotated_image_job


def _error_payload(message):
    return {"error": str(message), "results_by_image": {}}


def main():
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except Exception as exc:
        print(json.dumps(_error_payload(f"Invalid input JSON: {exc}")))
        return 1

    images = payload.get("images", [])
    if not isinstance(images, list):
        print(json.dumps(_error_payload("Input field 'images' must be an array")))
        return 1

    results_by_image = {}
    for item in images:
        absolute_path = str((item or {}).get("absolutePath") or "").strip()
        display_name = str((item or {}).get("displayName") or "").strip() or absolute_path
        if not absolute_path:
            results_by_image[display_name or "unknown"] = {"error": "Missing absolutePath"}
            continue
        try:
            with open(absolute_path, "rb") as f:
                image_bytes = f.read()
            done_name, result = process_rotated_image_job(display_name, image_bytes)
            results_by_image[done_name] = result
        except Exception as exc:
            results_by_image[display_name] = {"error": str(exc)}

    print(json.dumps({"results_by_image": results_by_image}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
