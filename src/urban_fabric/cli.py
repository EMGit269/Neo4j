"""Command line workflow. Running demo never accesses OSM or a database."""

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

import requests
from dotenv import load_dotenv
from neo4j.exceptions import DriverError, Neo4jError

from .config import load_config
from .demo import make_demo
from .fetch import fetch_osm
from .graph import export_graph
from .metrics import analyze
from .neo4j_store import import_graph


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="OSM → 城市形态指标 → Neo4j 空间图")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ["fetch", "analyze", "export-graph", "run", "prepare-clean"]:
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, required=True)
    sub = commands.add_parser("demo", help="离线合成示例，验证全流程至图文件")
    sub.add_argument("--output", type=Path, default=Path("data/demo"))
    sub = commands.add_parser("fetch-gba", help="按区界获取 GBA 建筑轮廓及估计高度")
    sub.add_argument("--config", type=Path, required=True)
    sub.add_argument("--process-only", action="store_true", help="校验并处理已有完整分块")
    sub = commands.add_parser("add-osm-streets", help="给已获取的 GBA 样例补充 OSM 路网")
    sub.add_argument("--data-dir", type=Path, required=True)
    sub.add_argument("--timeout", type=int, default=180)
    sub = commands.add_parser("import-neo4j")
    sub.add_argument("--graph-dir", type=Path, required=True)
    sub.add_argument("--database")
    sub.add_argument("--batch-size", type=int, default=500)
    sub.add_argument("--dry-run", action="store_true", help="仅检查文件，不连接数据库")
    sub.add_argument(
        "--replace-study-area",
        action="store_true",
        help="先删除相同 study_area_id 的已有数据，再导入；其他研究区不变",
    )
    commands.add_parser("doctor", help="显示依赖版本，不检查外部服务")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    load_dotenv()
    try:
        if args.command == "doctor":
            result = {
                name: version(name)
                for name in ["urban-fabric-lab", "geopandas", "osmnx", "momepy", "neo4j", "shapely"]
            }
        elif args.command == "import-neo4j":
            result = import_graph(
                args.graph_dir,
                database=args.database,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                replace_study_area=args.replace_study_area,
            )
        elif args.command == "demo":
            settings = make_demo(args.output.resolve())
            _, summary = analyze(settings)
            result = {"summary": summary, "graph": export_graph(settings)}
        elif args.command == "fetch-gba":
            from .gba_acquire import fetch_gba

            result = fetch_gba(
                args.config,
                process_only=args.process_only,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        elif args.command == "add-osm-streets":
            from .gba_streets import add_osm_streets

            result = add_osm_streets(args.data_dir, timeout=args.timeout)
        elif args.command == "prepare-clean":
            from .cleaning import prepare_clean_dataset

            result = prepare_clean_dataset(args.config)
        else:
            settings = load_config(args.config)
            if args.command == "fetch":
                layers = fetch_osm(settings)
                result = {
                    "raw_dir": str(settings.data_dir / "raw"),
                    "buildings": len(layers.buildings),
                    "street_segments": len(layers.street_segments),
                }
            elif args.command == "analyze":
                _, result = analyze(settings)
            elif args.command == "export-graph":
                result = export_graph(settings)
            else:
                fetch_osm(settings)
                _, summary = analyze(settings)
                result = {"summary": summary, "graph": export_graph(settings)}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, DriverError, Neo4jError, requests.RequestException) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
