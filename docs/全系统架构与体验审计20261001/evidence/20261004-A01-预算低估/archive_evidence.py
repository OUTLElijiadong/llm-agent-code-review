"""Archive this task's local evidence only; no model, database or network calls."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path


def main() -> None:
    evidence = Path(__file__).resolve().parent
    project = evidence.parents[3]
    for number in (1, 2, 3):
        source = Path(f"/tmp/prism-a01-full-related-frozen-{number}-20261004.log")
        text = source.read_text()
        assert re.search(r"667 passed, 2 warnings", text), number
        shutil.copyfile(source, evidence / f"冻结关联-667项-第{number}轮.log")
        final_source = Path(f"/tmp/prism-a01-full-related-adapter-{number}-20261004.log")
        assert re.search(r"709 passed, 2 warnings", final_source.read_text()), number
        shutil.copyfile(final_source, evidence / f"最终关联-709项-第{number}轮.log")
    additional_logs = {
        "/tmp/prism-a01-full-suite-boundary-red-20261004.log": "完整集追加边界-9红19绿.log",
        "/tmp/prism-a01-review-quota-old-adapter-red-20261004.log": "受控重放旧审查配额-6红.log",
        "/tmp/prism-a01-review-unit-adapter-green-20261004.log": "追加-单位修复后仍32k拒绝.log",
        "/tmp/prism-a01-boundary-first-green-20261004.log": "追加-夹具校准中间失败.log",
        "/tmp/prism-a01-boundary-expanded-green-20261004.log": "追加核心-42项.log",
    }
    for name, target in additional_logs.items():
        shutil.copyfile(Path(name), evidence / target)

    files = [
        "backend/app/services/deepseek_responses_runtime.py",
        "backend/tests/unit/services/test_responses_context_token_budget.py",
        "backend/tests/unit/services/test_deepseek_responses_runtime.py",
        "backend/tests/unit/services/test_deepseek_responses_service.py",
        "backend/tests/unit/agents/test_chat_agent_coverage.py",
        "backend/tests/unit/test_discussion_orchestrator_coverage.py",
        "backend/app/services/review_service.py",
        "backend/tests/unit/agents/test_c12_context_projection.py",
        "backend/tests/unit/agents/test_c13_semantic_coverage.py",
        "backend/tests/unit/services/test_review_non_code_compaction.py",
    ]
    source_hashes = {name: hashlib.sha256((project / name).read_bytes()).hexdigest() for name in files}
    assert source_hashes[files[0]] == "87fec56120f24e70daffcde19926a333275270e72a87f6dea0df0cb293e526af"
    metadata = {
        "状态": "候选源码与测试冻结；生产与付费模型不在本轮通过范围",
        "产品文件": [files[0], files[6]], "测试文件": files[1:6] + files[7:], "源与测试摘要": source_hashes,
        "初始新用例": {"失败": 8, "正常对照": 2, "结构Schema漏预算首次失败": 1},
        "重试协议红证据": "受控重放可信旧commit单method，使用新估算器；不是自然首次测试",
        "第一阶段回归": {"核心": 83, "关联文件": 23, "每轮": 667, "重复轮数": 3,
                    "耗时秒": [45.65, 46.91, 175.58], "性能含义": "无，第三轮与父代理完整后端并行"},
        "完整集追加发现": {"首次完整后端": {"失败": 9, "通过": 5768, "跳过": 5},
                         "自然复现3文件": {"失败": 9, "正常对照": 19},
                         "单位适配6红": "受控重放可信HEAD旧单函数，当前字节估算器，本地桩；不是自然首次运行"},
        "最终回归": {"追加核心": 42, "关联文件": 26, "每轮": 709, "重复轮数": 3,
                    "耗时秒": [11.88, 11.76, 11.66], "性能含义": "无；各阶段有重叠，不相加为不重复总数"},
        "官方本地tokenizer": {
            "实际成功重计量": 2, "第三次": "公开文件下载TLS EOF/连接超时；未完成计量",
            "模型": "DeepSeek-V4.1-Flash", "revision": "2cba9e42aa026125f3ed06c6d98c1db82f7ca027",
            "原文内容token合计": [1060022, 1060022, 1080022],
            "供应商API调用": 0, "计费usage": None, "语义理解验收": False,
            "候选依赖变更": False, "计量样本限制": "相同合成样本的重复，不代表自然语义复杂度",
        },
        "来源封装额外检查": {"样本": 48, "最大预算": 47991, "预算": 48000, "复现缺陷": False},
        "工具边界": [
            "曾误写不存在测试路径而0测试；未计通过",
            "新fixture误把Runtime fallback32768当生产settings65536，2失败后校正为两套参数各自验证；未改产品默认",
            "第三次公开下载严格TLS失败；未关闭证书校验、未换供应商、临时目录finally删除",
            "追加日志原文件名green但实际1fail，以及一次夹具相同行误命中后校正日志均保留，未计通过",
        ],
        "未完成": ["真实供应商百万token语义验收", "各业务入口实际计费/权限/工作集流程", "真手机与全站账号回归"],
        "证据文件": [],
    }
    for path in sorted(evidence.iterdir()):
        if path.is_file() and path.name != "证据清单.json":
            metadata["证据文件"].append({
                "名称": path.name, "字节": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    (evidence / "证据清单.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"product_files": 2, "test_files": 8, "additional_core": 42,
                      "related": 709, "rounds": 3, "evidence_files": len(metadata["证据文件"])}))


if __name__ == "__main__":
    main()
