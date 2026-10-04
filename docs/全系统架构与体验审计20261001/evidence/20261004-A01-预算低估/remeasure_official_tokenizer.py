"""Remeasure public synthetic inputs with a pinned official tokenizer.

Run with candidate backend/.venv311/bin/python. Downloads only JSON tokenizer
data and the official PyPI macOS arm64 wheel into a new private temporary tree.
No weights, trust_remote_code, API key, model calls or database operations.
The tree is removed in finally; stdout contains counts and hashes only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REVISION = "2cba9e42aa026125f3ed06c6d98c1db82f7ca027"
REPO = "deepseek-ai/DeepSeek-V4.1-Flash"
ARTIFACTS = {
    "tokenizer.json": (
        f"https://huggingface.co/{REPO}/resolve/{REVISION}/tokenizer.json",
        "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
    ),
    "tokenizer_config.json": (
        f"https://huggingface.co/{REPO}/resolve/{REVISION}/tokenizer_config.json",
        "6ac8c8dc065ed118161d02dd532749ae3f52c243deac27872134fae2f50d8547",
    ),
    "tokenizers-0.23.2-cp310-abi3-macosx_11_0_arm64.whl": (
        "https://files.pythonhosted.org/packages/67/49/22da045a91732384d3a3771816bf188dc5a1f702c32e635afa7c679c0bef/"
        "tokenizers-0.23.2-cp310-abi3-macosx_11_0_arm64.whl",
        "986670e43691469dcee610ea0f846f91a8f84e91fc6f7a48d4c064414c0ec2bf",
    ),
}


def main() -> int:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("This frozen tool wheel is only for macOS arm64; select and verify a platform wheel separately.")
    project = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(project / "backend"))
    scratch = Path(tempfile.mkdtemp(prefix="prism-a01-official-tokenizer-remeasure-"))
    result = {"repo": REPO, "revision": REVISION, "kind": "official_local_content_tokenizer_not_provider_usage",
              "platform": platform.platform(), "paid_model_calls": 0, "database_operations": 0,
              "artifacts": {}, "small_samples": [], "large_samples": []}
    try:
        for name, (url, expected) in ARTIFACTS.items():
            download_method = "stdlib-https"
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    raw = response.read()
            except urllib.error.URLError:
                # Retry the same pinned public artifact with system curl. TLS
                # verification remains enabled; its checksum is still mandatory.
                download_method = "system-curl-https-after-stdlib-error"
                subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                                "--retry", "2", "--connect-timeout", "20", "--max-time", "60",
                                "--output", str(scratch / name), url], check=True)
                raw = (scratch / name).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != expected:
                raise RuntimeError(f"Public artifact hash mismatch: {name}")
            (scratch / name).write_bytes(raw)
            result["artifacts"][name] = {"sha256": digest, "bytes": len(raw), "url": url,
                                         "download_method": download_method}
        subprocess.run([sys.executable, "-m", "venv", str(scratch / "venv")], check=True,
                       stdout=subprocess.DEVNULL)
        wheel = next(scratch.glob("*.whl"))
        subprocess.run([str(scratch / "venv/bin/python"), "-m", "pip", "install", "--no-index",
                        "--no-deps", str(wheel)], check=True, stdout=subprocess.DEVNULL)
        sites = list((scratch / "venv/lib").glob("python*/site-packages"))
        if len(sites) != 1:
            raise RuntimeError("Unexpected isolated venv layout")
        sys.path.insert(0, str(sites[0]))
        from tokenizers import Tokenizer
        from app.services.deepseek_responses_runtime import compact_transcript, estimate_tokens

        tokenizer_definition = json.loads((scratch / "tokenizer.json").read_text())
        result["tokenizer_model_type"] = tokenizer_definition.get("model", {}).get("type")
        result["tokenizer_byte_fallback"] = tokenizer_definition.get("model", {}).get("byte_fallback")
        tokenizer = Tokenizer.from_file(str(scratch / "tokenizer.json"))
        source = project / "backend/app/services/deepseek_responses_runtime.py"
        result["runtime_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        for label, unit in [("words", " a"), ("punctuation", ".a"), ("numbers", " 7"),
                            ("json", '{"a":1},'), ("code", "x=1;\n"),
                            ("chinese", "信息"), ("rare-chinese", "龘")]:
            text = unit * 10_000
            count = len(tokenizer.encode(text, add_special_tokens=False).ids)
            budget = estimate_tokens(text)
            result["small_samples"].append({"sample": label, "official_local_content_tokens": count,
                                           "application_budget": budget, "upper_bound_holds": budget >= count,
                                           "input_sha256": hashlib.sha256(text.encode()).hexdigest()})
        for label, unit, per, total in [("million-numbers", " 7", 26_500, 20),
                                       ("million-words", " a", 26_500, 40),
                                       ("million-code", "x=1;\n", 10_000, 27)]:
            history = [{"role": "user", "content": "Analyse the supplied material. Only provide a concise read-only answer."}]
            history += [{"role": "user", "content": unit * per} for _ in range(total)]
            history += [{"role": "user", "content": "Return a concise summary of the material."}]
            before = copy.deepcopy(history)
            projected, metadata = compact_transcript(
                history, context_window_tokens=1_000_000, max_output_tokens=65_536,
                compaction_threshold_tokens=850_000, keep_recent_tokens=200_000, overhead_tokens=4096,
                semantic_summary="[local planning fixture; no model semantic claim]",
            )
            serialized = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
            result["large_samples"].append({
                "sample": label, "messages": len(history),
                "largest_message_chars": max(len(item["content"]) for item in history),
                "official_local_content_tokens_sum": sum(len(tokenizer.encode(item["content"], add_special_tokens=False).ids)
                                                         for item in history),
                "official_local_serialized_history_tokens": len(tokenizer.encode(serialized, add_special_tokens=False).ids),
                "application_budget": estimate_tokens(history), "compacted": metadata["compacted"],
                "projected_budget": estimate_tokens(projected),
                "projected_fits_reserved_budget": estimate_tokens(projected) + 65_536 + 4096 <= 1_000_000,
                "original_unchanged": history == before,
                "input_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            })
        result["budget_passed"] = all(item["upper_bound_holds"] for item in result["small_samples"]) and all(
            item["compacted"] and item["original_unchanged"] and item["projected_fits_reserved_budget"]
            for item in result["large_samples"])
    finally:
        # Only the exact tree created in this process is removed.
        shutil.rmtree(scratch)
        result["temporary_tree_removed"] = not scratch.exists()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["budget_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
