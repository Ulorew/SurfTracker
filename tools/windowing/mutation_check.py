#!/usr/bin/env python3
"""Мутационный прогон.

Единственный известный способ отличить тест, который что-то проверяет, от
теста, который просто выполняется. Меняет исходник в одном месте и смотрит,
упадут ли тесты: упали — мутант убит, правило действительно защищено; прошли
— в этом месте кода тестов нет, что бы ни говорило покрытие.

    python mutation_check.py --files track_logic.py track_kalman.py
    python mutation_check.py --all-new          # модули сегодняшней ночи
    python mutation_check.py --files X --tests tests/test_x.py -j 8

Работает в песочнице: копия .py-файлов в отдельной папке, оригиналы не
трогаются вообще (правка файла на месте с восстановлением в finally рано или
поздно оставляет мутацию в рабочем дереве — так уже было в этом проекте).
"""
import argparse
import ast
import concurrent.futures
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Модули, написанные/переписанные в ночной серии: по регламенту выживший
# мутант здесь — блокер, в остальном коде идёт в список долга.
NEW_TONIGHT = ["angles.py", "track_kalman.py", "track_logic.py", "track_filters.py",
               "sample_window.py", "online_dataset.py", "dataset_gen.py", "track_run.py"]

CMP_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}
BIN_SWAP = {
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.Div, ast.Div: ast.Mult,
}
BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


class _Collector(ast.NodeVisitor):
    """Перечисляет места, которые можно мутировать: (тип, id узла, описание)."""

    def __init__(self):
        self.sites = []

    def visit_Compare(self, node):
        for i, op in enumerate(node.ops):
            if type(op) in CMP_SWAP:
                self.sites.append(("cmp", (node.lineno, node.col_offset, i),
                                    f"{type(op).__name__} -> {CMP_SWAP[type(op)].__name__}"))
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if type(node.op) in BIN_SWAP:
            self.sites.append(("bin", (node.lineno, node.col_offset, 0),
                                f"{type(node.op).__name__} -> {BIN_SWAP[type(node.op)].__name__}"))
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        if type(node.op) in BOOL_SWAP:
            self.sites.append(("bool", (node.lineno, node.col_offset, 0),
                                f"{type(node.op).__name__} -> {BOOL_SWAP[type(node.op)].__name__}"))
        self.generic_visit(node)

    def visit_Constant(self, node):
        v = node.value
        if isinstance(v, bool):
            self.sites.append(("const_bool", (node.lineno, node.col_offset, 0),
                                f"{v} -> {not v}"))
        elif isinstance(v, (int, float)) and v not in (0, 1) and abs(v) < 1e6:
            self.sites.append(("const_num", (node.lineno, node.col_offset, 0),
                                f"{v} -> {v * 1.5:g}"))
        self.generic_visit(node)


class _Mutator(ast.NodeTransformer):
    def __init__(self, kind, site):
        self.kind, self.site = kind, site
        self.applied = False

    def _key(self, node, i=0):
        return (node.lineno, node.col_offset, i)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.kind == "cmp":
            for i, op in enumerate(node.ops):
                if self._key(node, i) == self.site and type(op) in CMP_SWAP:
                    node.ops[i] = CMP_SWAP[type(op)]()
                    self.applied = True
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if self.kind == "bin" and self._key(node) == self.site and type(node.op) in BIN_SWAP:
            node.op = BIN_SWAP[type(node.op)]()
            self.applied = True
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self.kind == "bool" and self._key(node) == self.site and type(node.op) in BOOL_SWAP:
            node.op = BOOL_SWAP[type(node.op)]()
            self.applied = True
        return node

    def visit_Constant(self, node):
        if self._key(node) != self.site:
            return node
        if self.kind == "const_bool" and isinstance(node.value, bool):
            self.applied = True
            return ast.copy_location(ast.Constant(value=not node.value), node)
        if self.kind == "const_num" and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            self.applied = True
            return ast.copy_location(ast.Constant(value=node.value * 1.5), node)
        return node


def collect_sites(path):
    tree = ast.parse(open(path).read())
    c = _Collector()
    c.visit(tree)
    return c.sites


def mutate_source(path, kind, site):
    tree = ast.parse(open(path).read())
    m = _Mutator(kind, site)
    tree = m.visit(tree)
    if not m.applied:
        return None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def make_sandbox(dst):
    """Только .py: тестам больше ничего из папки не нужно, а копировать
    output/ с видео — минуты и гигабайты."""
    os.makedirs(os.path.join(dst, "tests"), exist_ok=True)
    for name in os.listdir(HERE):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(HERE, name), os.path.join(dst, name))
    for name in os.listdir(os.path.join(HERE, "tests")):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(HERE, "tests", name),
                         os.path.join(dst, "tests", name))
    return dst


def run_tests(sandbox, tests, timeout):
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
           "--no-header", "-o", "addopts="] + tests
    try:
        r = subprocess.run(cmd, cwd=sandbox, capture_output=True, text=True,
                            timeout=timeout)
    except subprocess.TimeoutExpired:
        # зависание — тоже смерть мутанта (бесконечный цикл ловится тестом)
        return True, "таймаут"
    return r.returncode != 0, (r.stdout or "")[-400:]


def check_one(args):
    src_name, kind, site, desc, tests, timeout = args
    sandbox = tempfile.mkdtemp(prefix="mut_")
    try:
        make_sandbox(sandbox)
        mutated = mutate_source(os.path.join(HERE, src_name), kind, site)
        if mutated is None:
            return None
        open(os.path.join(sandbox, src_name), "w").write(mutated)
        killed, tail = run_tests(sandbox, tests, timeout)
        return {"file": src_name, "line": site[0], "kind": kind, "desc": desc,
                "killed": killed, "tail": None if killed else tail}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", default=None)
    ap.add_argument("--all-new", action="store_true",
                     help=f"модули ночной серии: {' '.join(NEW_TONIGHT)}")
    ap.add_argument("--tests", nargs="+", default=["tests"])
    ap.add_argument("--jobs", "-j", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--limit-per-file", type=int, default=None,
                     help="выборка мутантов на файл (по умолчанию все)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    files = args.files or (NEW_TONIGHT if args.all_new else None)
    if not files:
        ap.error("нужен --files или --all-new")

    # Базовый прогон: на чистом коде тесты обязаны проходить, иначе "убитый
    # мутант" ничего не значит — падало бы и без мутации.
    base_sandbox = tempfile.mkdtemp(prefix="mut_base_")
    try:
        make_sandbox(base_sandbox)
        failed, tail = run_tests(base_sandbox, args.tests, args.timeout)
    finally:
        shutil.rmtree(base_sandbox, ignore_errors=True)
    if failed:
        print("НЕЛЬЗЯ НАЧИНАТЬ: тесты падают и без мутаций\n" + tail)
        return 2

    tasks, dropped = [], {}
    rng = random.Random(args.seed)
    for f in files:
        sites = collect_sites(os.path.join(HERE, f))
        if args.limit_per_file and len(sites) > args.limit_per_file:
            keep = rng.sample(sites, args.limit_per_file)
            dropped[f] = len(sites) - len(keep)
            sites = keep
        for kind, site, desc in sites:
            tasks.append((f, kind, site, desc, args.tests, args.timeout))

    for f, n in dropped.items():
        print(f"ВНИМАНИЕ: в {f} проверено не всё — {n} мутантов отброшено выборкой")
    print(f"мутантов: {len(tasks)}, потоков: {args.jobs}")

    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for i, res in enumerate(ex.map(check_one, tasks), 1):
            if res is None:
                continue
            results.append(res)
            if i % 20 == 0 or not res["killed"]:
                alive = sum(1 for r in results if not r["killed"])
                print(f"[{i}/{len(tasks)}] выживших {alive}"
                      + ("" if res["killed"]
                         else f"  <- {res['file']}:{res['line']} {res['desc']}"),
                      flush=True)

    print("\n=== мутационный счёт ===")
    by_file = {}
    for r in results:
        st = by_file.setdefault(r["file"], [0, 0])
        st[0] += 1
        st[1] += 1 if r["killed"] else 0
    for f in sorted(by_file):
        total, killed = by_file[f]
        print(f"  {f:24s} {killed}/{total} убито ({100.0 * killed / total:.0f}%)")

    survivors = [r for r in results if not r["killed"]]
    print(f"\n=== выжившие ({len(survivors)}) ===")
    for r in survivors:
        print(f"  {r['file']}:{r['line']}  {r['kind']}  {r['desc']}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"files": files, "tests": args.tests, "dropped": dropped,
                       "results": results}, fh, indent=2, ensure_ascii=False)
        print("\nподробности:", args.out)
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
