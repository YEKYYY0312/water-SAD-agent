"""对阿里云百炼做单条 embedding 烟囱验证。

用法：
  --dry-run            只校验 Provider 配置，不调用云端（无需真实 Key）
  默认                  真实调用，需先 export DASHSCOPE_API_KEY
"""
from __future__ import annotations

import argparse
import json

from water_agent.embedding import BailianEmbeddingConfig, BailianEmbeddingProvider, build_embedding_provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a single Bailian embedding call")
    parser.add_argument("--text", default="给水排水工程设计中，生活给水管道的设计秒流量应如何确定？")
    parser.add_argument("--dimensions", type=int, default=512, choices=[256, 512, 768, 1024])
    parser.add_argument("--dry-run", action="store_true", help="do not call remote; only validate config")
    args = parser.parse_args(argv)

    if args.dry_run:
        cfg = BailianEmbeddingConfig(api_key="dry-run-no-key", dimensions=args.dimensions)
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "model": cfg.model,
            "dimensions": cfg.dimensions,
            "index_version": cfg.index_version,
            "fallback_enabled": cfg.fallback_enabled,
            "message": "Provider 配置有效；未调用云端，也未创建索引。",
        }, ensure_ascii=False, indent=2))
        return 0

    provider = build_embedding_provider(dimensions=args.dimensions)
    vector = provider.embed(args.text)
    print(json.dumps({
        "ok": True,
        "model": provider.config.model,
        "dimensions": len(vector),
        "expected_dimensions": provider.config.dimensions,
        "index_version": provider.config.index_version,
        "message": f"{len(vector)} 维 embedding 成功返回。",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
