"""检测 FastGPT 对接：连通性、可访问的知识库，以及自定义分隔符分块是否按预期生效。

在部署 Stratum 的服务器上运行（读取 stratum.env 或 /etc/stratum/stratum.env 中的 FastGPT 配置）：

    .venv/bin/python deploy/fastgpt_probe.py                 # 只检查连通性并列出知识库
    .venv/bin/python deploy/fastgpt_probe.py --dataset <ID>  # 另外在该知识库中做分块测试

分块测试会创建名为 stratum-probe-* 的临时集合（每个约 1500 字）。FastGPT V4.9 起在后台解析分块、生成向量，
脚本每 3 秒读一次集合中的数据条数，最多等 --wait 秒（默认 180），读到后立即删除临时集合。
请把最后输出的「检测结果」整段发给开发者；输出中不包含 API 密钥。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env():
    for path in (ROOT / "stratum.env", Path("/etc/stratum/stratum.env")):
        if not path.is_file():
            continue
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return str(path)
    return None


def wait_for_chunks(client, collection_id: str, insert_len, wait: int):
    """FastGPT 同步返回了块数就直接用；否则轮询集合中的数据条数，直到连续 15 秒不再变化
    （不在达到 3 条时提前结束，以免分块方式不对、还在继续生成时误判为正确）。"""
    import time
    if insert_len:
        return insert_len
    end = time.time() + wait
    last, stable_since = None, time.time()
    while time.time() < end:
        count = client.count_data(collection_id) or 0
        if count != last:
            print(f"    已生成 {count} 条数据…", flush=True)
            last, stable_since = count, time.time()
        if count and time.time() - stable_since >= 15:
            return count
        time.sleep(3)
    return last or 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", help="用于分块测试的 FastGPT 知识库 ID")
    parser.add_argument("--wait", type=int, default=180, help="每种分块方式最多等待 FastGPT 后台处理的秒数")
    args = parser.parse_args()
    env_file = load_env()

    from app import fastgpt
    from app.storage import StorageError

    report: dict = {"env_file": env_file, "url": fastgpt.base_url() or None,
                    "api_key_set": bool(os.environ.get("STRATUM_FASTGPT_API_KEY"))}
    print(f"配置文件：{env_file or '未找到，使用当前环境变量'}")
    print(f"FastGPT 地址：{report['url'] or '未配置'}；API 密钥：{'已配置' if report['api_key_set'] else '未配置'}")
    if not fastgpt.configured():
        print("请先在配置文件中设置 STRATUM_FASTGPT_URL 和 STRATUM_FASTGPT_API_KEY。")
        return 1
    client = fastgpt.Client()

    try:
        datasets = client.list_datasets()
        report["datasets"] = [{"id": d["id"], "name": d["name"], "type": d["type"]} for d in datasets]
        print(f"\n可访问的知识库（{len(datasets)} 个）：")
        for d in datasets:
            print(f"  {d['id']}  {d['name']}  [{d['type']}]  向量模型：{d.get('vector_model') or '-'}")
    except StorageError as exc:
        report["datasets_error"] = str(exc)
        print(f"\n列出知识库失败：{exc}")

    if args.dataset:
        separator = "===CHUNK==="
        long_chunk = "> 章节：探测 > 长段落\n\n" + "这是用于检测分块方式的长段落，" * 100  # 约 1500 字
        chunks = ["> 章节：探测 > 第一块\n\n第一块内容。", long_chunk, "> 章节：探测 > 第三块\n\n第三块内容。"]
        text = f"\n\n{separator}\n\n".join(chunks)
        print(f"\n分块测试：文本共 {len(text)} 字，按分隔符应得到 3 块（其中一块约 {len(long_chunk)} 字）")
        report["probe"] = []
        for mode in ("char", "size", "paragraph"):
            os.environ["STRATUM_FASTGPT_SPLIT_MODE"] = mode
            entry: dict = {"chunkSplitMode": mode, "payload": fastgpt.chunk_payload(separator, 800)}
            try:
                created = client.create_text(args.dataset, f"stratum-probe-{mode}", text, separator, 800,
                                             metadata={"source": "stratum-probe"})
                entry["insertLen"] = created["chunks"]
                entry["chunks"] = wait_for_chunks(client, created["collection_id"], created["chunks"], args.wait)
                try:
                    detail = client._request("GET", "/api/core/dataset/collection/detail",
                                             params={"id": created["collection_id"]})
                    if isinstance(detail, dict):
                        entry["stored"] = {k: detail.get(k) for k in (
                            "trainingType", "chunkSettingMode", "chunkSplitMode", "chunkSize", "indexSize",
                            "chunkSplitter", "chunkTriggerType") if k in detail}
                except StorageError as exc:
                    entry["detail_error"] = str(exc)
                try:
                    client.delete_collection(created["collection_id"])
                    entry["deleted"] = True
                except StorageError as exc:
                    entry["deleted"] = False
                    entry["delete_error"] = str(exc)
            except StorageError as exc:
                entry["error"] = str(exc)
            report["probe"].append(entry)
            got = entry.get("chunks")
            verdict = ("✓ 正好 3 块" if got == 3 else
                       f"✗ {args.wait} 秒内 FastGPT 没有生成数据（后台繁忙时可加大 --wait 重试）" if got == 0 else
                       f"✗ 得到 {got} 块" if "chunks" in entry else f"✗ 失败：{entry.get('error')}")
            print(f"  chunkSplitMode={mode}: {verdict}{'' if entry.get('deleted', True) else '（临时集合删除失败，请手动删除）'}")
        good = [e["chunkSplitMode"] for e in report["probe"] if e.get("chunks") == 3]
        report["recommended_split_mode"] = good[0] if good else None
        if good:
            print(f"\n建议：STRATUM_FASTGPT_SPLIT_MODE={good[0]}" + ("（默认值，无需配置）" if good[0] == "char" else ""))
        else:
            print("\n没有一种方式得到 3 块，请把下面的检测结果发给开发者。")

    print("\n===== 检测结果（请整段复制）=====")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
