#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf2md_mineru —— 用 MinerU 把 PDF 转成适合直接喂给 LLM 的 Markdown。

定位：独立程序，与本目录下的其他转换脚本无任何关系。

MinerU 的原始输出直接用于 LLM 有三个问题，本脚本针对性处理：
  1. 图片以 base64 内联在正文中，一个 30 KB 的图约等于 7 K token 的二进制噪声
  2. 泄漏出版商 CMS 的样式标签（<small><span class=... style=...>）
  3. 列表用字面「•」字符，严格 Markdown 解析器不认它是列表

此外修复 PDF 字体缺 ToUnicode 表导致的变音符脱落（Fr<diaeresis>olke）。

零第三方依赖，只用 Python 标准库。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

VERSION = "1.0.0"
TIERS = ("flash", "basic", "standard", "advanced")
DEFAULT_TIMEOUT = 600

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def die(code, msg):
    sys.stderr.write("错误：%s\n" % msg)
    sys.exit(code)


def force_utf8_stdout():
    """Windows 控制台默认 GBK，中文进度信息会花屏。强制 UTF-8 并容错。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def human(n):
    return "{:,}".format(n)


# ---------------------------------------------------------------- 定位 MinerU

def _exe_name():
    return "mineru-kit.exe" if os.name == "nt" else "mineru-kit"


def _subdir():
    return "Scripts" if os.name == "nt" else "bin"


def find_mineru(explicit):
    """按优先级定位 mineru-kit 可执行文件。全部未命中时报错并列出已尝试路径。"""
    exe = _exe_name()

    if explicit:
        p = Path(explicit)
        if p.is_dir():
            p = p / exe
        if not p.is_file():
            die(EXIT_USAGE, "--mineru 指定的路径无效（不存在或不是文件）：%s" % explicit)
        return str(p)

    env = os.environ.get("MINERU_KIT_EXE", "").strip()
    if env:
        p = Path(env)
        if p.is_dir():
            p = p / exe
        if p.is_file():
            return str(p)
        die(EXIT_USAGE, "环境变量 MINERU_KIT_EXE 指向的路径无效：%s" % env)

    found = shutil.which("mineru-kit") or shutil.which(exe)
    if found:
        return found

    tried = []
    here = Path(__file__).resolve().parent
    homes = [here]
    for h in (Path.home(),):
        if h not in homes:
            homes.append(h)

    # 已知常见位置
    for base in homes:
        for rel in (
            Path("mineru") / ".venv" / _subdir() / exe,
            Path(".venv") / _subdir() / exe,
            Path("venv") / _subdir() / exe,
        ):
            c = base / rel
            tried.append(str(c))
            if c.is_file():
                return str(c)

    # 有限广搜：深度 <= 4，跳过重量级目录
    skip = {"node_modules", ".git", "AppData", "site-packages", "__pycache__",
            ".cache", ".npm", ".cargo", ".rustup"}
    for base in homes:
        base_depth = len(base.parts)
        for root, dirs, files in os.walk(str(base)):
            rp = Path(root)
            dirs[:] = [d for d in dirs if d not in skip]
            if len(rp.parts) - base_depth > 4:
                dirs[:] = []
                continue
            if exe in files:
                return str(rp / exe)

    msg = [
        "找不到 MinerU（mineru-kit）。已尝试：",
        "  1. --mineru 显式指定",
        "  2. 环境变量 MINERU_KIT_EXE",
        "  3. PATH",
    ]
    for t in tried:
        msg.append("  - " + t)
    msg += [
        "",
        "解决办法（任选其一）：",
        "  a. 安装 MinerU 到 PATH：pip install mineru-kit",
        "  b. 设环境变量：setx MINERU_KIT_EXE \"<路径>\\.venv\\Scripts\\mineru-kit.exe\"",
        "  c. 本次运行指定：pdf2md_mineru.py <文件> --mineru \"<路径>\"",
    ]
    die(EXIT_USAGE, "\n".join(msg))


# ---------------------------------------------------------------- 调用 MinerU

def run_mineru(mineru, pdf, tier, timeout, out_dir):
    """在 out_dir 中调用 MinerU 解析。

    返回 (产出的 .md 路径, 错误信息)，二者其一为 None。
    out_dir 由调用方创建并负责清理，本函数不碰目录生命周期。
    """
    cmd = [mineru, "parse", str(pdf), "-o", str(out_dir), "--tier", tier]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "转换超时（超过 %d 秒）" % timeout
    except OSError as e:
        return None, "无法启动 MinerU：%s" % e

    if proc.returncode != 0:
        blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
        lines = [x for x in blob.strip().splitlines() if x.strip()]
        tail = "\n".join(lines[-12:])
        return None, "MinerU 退出码 %d\n%s" % (proc.returncode, tail)

    md = _find_output_md(out_dir, pdf)
    if md is None:
        return None, "MinerU 未产出 .md 文件（输出目录：%s）" % out_dir
    return md, None


def _find_output_md(out_dir, pdf):
    p = Path(out_dir)
    exact = p / (pdf.stem + ".md")
    if exact.is_file():
        return exact
    cands = sorted(p.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def read_text(path):
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def write_text(path, text):
    Path(path).write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- 后处理

RE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
RE_DROP_OPEN = re.compile(r"<\s*(?:small|span)\b[^>]*>", re.I)
RE_DROP_CLOSE = re.compile(r"<\s*/\s*(?:small|span)\s*>", re.I)
RE_TAG = re.compile(r"<\s*(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s[^<>]*?)?)(/?)\s*>")
RE_ATTR = re.compile(r"""<(?!/)[a-zA-Z][^<>]*?\s(?:class|style|data-[\w-]+|width|height|align|colspan|rowspan|valign|bgcolor|border)\s*=""", re.I)
RE_BULLET = re.compile(r"(?m)^([ \t]*)([•·‣∙⁃●○▪■])([ \t]+)")
RE_FENCE = re.compile(r"(?ms)^(`{3,}|~{3,}).*?^\1[ \t]*$")
RE_BLANKS = re.compile(r"\n{3,}")
RE_TRAIL = re.compile(r"[ \t]+$", re.M)
VOWELS = "aeiouyAEIOUY"


def _segments(text):
    """把文本切成「代码块」与「非代码块」交替的片段，供逐段处理。"""
    pos = 0
    for m in RE_FENCE.finditer(text):
        if m.start() > pos:
            yield False, text[pos:m.start()]
        yield True, m.group(0)
        pos = m.end()
    if pos < len(text):
        yield False, text[pos:]


def step_images(text, report):
    """把一切图片引用换成 [图片 N] 占位符，原始目标记入日志。"""
    counter = [0]

    def repl(m):
        alt, target = m.group(1).strip(), m.group(2).strip()
        counter[0] += 1
        if target.startswith("data:"):
            kind = "内嵌 base64"
            size = "约 %s B" % human(int(len(target) * 3 / 4)) if "," in target else "?"
        elif target.lower().startswith(("http://", "https://")):
            kind = "外部链接"
            size = target
        elif target:
            kind, size = "本地文件", target
        else:
            kind, size = "空引用", alt or "(无 alt)"
        report["images"].append({
            "n": counter[0], "kind": kind, "target": size, "alt": alt,
        })
        return "[图片 %d]" % counter[0]

    out = RE_IMAGE.sub(repl, text)
    report["stat"]["images"] = counter[0]
    return out


def step_html(text, report):
    """删除 <small>/<span>（保留内文），其余标签保留但洗掉全部属性。

    MinerU 会把复杂表格输出成 HTML <table>，那是语义内容，必须保留；
    要清掉的只是 class/style/data-* 这类表现层属性。
    """
    before = len(RE_TAG.findall(text))
    attrs_before = len(RE_ATTR.findall(text))

    def clean_chunk(chunk):
        chunk = RE_DROP_OPEN.sub("", chunk)
        chunk = RE_DROP_CLOSE.sub("", chunk)

        def attr(m):
            return "<%s%s%s>" % (m.group(1), m.group(2), m.group(4))

        return RE_TAG.sub(attr, chunk)

    out = "".join(c if fenced else clean_chunk(c) for fenced, c in _segments(text))
    after = len(RE_TAG.findall(out))
    report["stat"]["html_before"] = before
    report["stat"]["html_after"] = after
    report["stat"]["html_attrs"] = attrs_before
    return out


def step_bullets(text, report):
    """行首的字面项目符号改成 Markdown 无序列表标记。"""
    n = [0]

    def repl(m):
        n[0] += 1
        return "%s- " % m.group(1)

    out = RE_BULLET.sub(repl, text)
    report["stat"]["bullets"] = n[0]
    return out


def step_diaeresis(text, report):
    """修复 PDF 缺 ToUnicode 表导致的变音符脱落。

    两种形态：
      Fr<diaeresis>olke    变音符跑到字母前面 —— 与后一个元音合并（Frölke）
      COVID-19 <dia> pandemic  跨行时的孤立残片 —— 直接删除，并收敛它带出的多余空格
    """
    n = [0]

    def bump(m, repl):
        n[0] += 1
        return repl

    # 1) 变音符紧跟某个元音 -> 合成为预组合字符
    out = re.sub(
        "¨([" + VOWELS + r"])",
        lambda m: bump(m, unicodedata.normalize("NFC", m.group(1) + "̈")),
        text,
    )

    # 2) 词与词之间的孤立变音符 -> 连同两侧空格压成单个空格
    out = re.sub(r"(?<=\S)[ \t]+¨[ \t]+(?=\S)",
                 lambda m: bump(m, " "), out)

    # 3) 行首 / 行尾的孤立变音符 -> 删除（不带空格，留给已有空白）
    out = re.sub(r"¨[ \t]+", lambda m: bump(m, ""), out)
    out = re.sub(r"[ \t]+¨", lambda m: bump(m, ""), out)

    # 4) 仍存在的（紧贴其他字符）-> 裸删
    if "¨" in out:
        cnt = out.count("¨")
        out = out.replace("¨", "")
        n[0] += cnt

    report["stat"]["diaeresis"] = n[0]
    return out


def step_tidy(text, report):
    """收敛空白：去行尾空格、压缩多余空行、保证单个结尾换行。代码块内不动。"""
    parts = []
    for fenced, chunk in _segments(text):
        if fenced:
            parts.append(chunk)
            continue
        chunk = RE_TRAIL.sub("", chunk)
        chunk = RE_BLANKS.sub("\n\n", chunk)
        parts.append(chunk)
    out = "".join(parts).strip("\n") + "\n"
    report["stat"]["blanks"] = 1
    return out


STEPS = (
    ("图片占位", step_images),
    ("HTML 清理", step_html),
    ("列表规范化", step_bullets),
    ("变音符修复", step_diaeresis),
    ("空白收敛", step_tidy),
)


def postprocess(text):
    report = {"images": [], "stat": {}}
    for _, fn in STEPS:
        text = fn(text, report)
    return text, report


# ---------------------------------------------------------------- 单文件流程

def convert_one(pdf, mineru, args):
    """转换单个 PDF，返回 (输出 .md 路径, 报告, 错误信息)。

    临时目录由本函数创建，无论成功失败都在 finally 清理；
    仅当 --keep-raw 时保留，并把路径记入报告供人工核对。
    """
    prefix = "pdf2md_mineru_raw_" if args.keep_raw else "pdf2md_mineru_"
    out_dir = tempfile.mkdtemp(prefix=prefix)
    try:
        t0 = time.time()
        md, err = run_mineru(mineru, pdf, args.tier, args.timeout, out_dir)
        if md is None:
            return None, None, err
        elapsed = time.time() - t0

        raw_text = read_text(md)
        if args.no_postprocess:
            out_text, report = raw_text, {"images": [], "stat": {}}
        else:
            out_text, report = postprocess(raw_text)
        report["elapsed"] = elapsed
        report["raw_bytes"] = len(raw_text.encode("utf-8"))

        dest = pdf.with_suffix(".md")
        write_text(dest, out_text)
        report["dest"] = dest
        report["out_bytes"] = Path(dest).stat().st_size
        if args.keep_raw:
            report["raw_dir"] = out_dir
        return dest, report, None
    finally:
        if not args.keep_raw:
            shutil.rmtree(out_dir, ignore_errors=True)


def print_one_report(pdf, report):
    st = report["stat"]
    print("  MinerU 用时 %.1fs | 原始 %s B -> 输出 %s B"
          % (report["elapsed"], human(report["raw_bytes"]), human(report["out_bytes"])))
    bits = []
    if st.get("images"):
        bits.append("图片占位 %d 处" % st["images"])
    if st.get("html_before"):
        if st["html_after"] < st["html_before"]:
            bits.append("HTML 标签 %d -> %d（删样式标签）"
                        % (st["html_before"], st["html_after"]))
        else:
            bits.append("HTML 标签 %d 个保留（表格），洗掉 %d 处属性"
                        % (st["html_after"], st.get("html_attrs", 0)))
    if st.get("bullets"):
        bits.append("列表符号 %d" % st["bullets"])
    if st.get("diaeresis"):
        bits.append("变音符 %d" % st["diaeresis"])
    if bits:
        print("  " + " | ".join(bits))
    if report.get("raw_dir"):
        print("  MinerU 原始输出已保留：%s" % report["raw_dir"])
    for img in report["images"]:
        print("    图 %d: %s | %s%s"
              % (img["n"], img["kind"], img["target"],
                 (" | alt=%s" % img["alt"]) if img["alt"] else ""))


# ---------------------------------------------------------------- 入口

def collect_pdfs(target):
    if target.is_file():
        return [target] if target.suffix.lower() == ".pdf" else []
    out = []
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("~$"):
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pdf2md_mineru.py",
        description="用 MinerU 把 PDF 转成适合喂给 LLM 的 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  pdf2md_mineru.py 论文.pdf\n"
               "  pdf2md_mineru.py ./papers/            递归处理目录下所有 PDF\n"
               "  pdf2md_mineru.py 论文.pdf --tier standard\n"
               "  pdf2md_mineru.py 论文.pdf --no-postprocess   只转不洗，用于对照\n",
    )
    ap.add_argument("target", help="PDF 文件，或包含 PDF 的目录")
    ap.add_argument("--tier", choices=TIERS, default="basic",
                    help="MinerU 解析档位，默认 basic")
    ap.add_argument("--mineru", metavar="路径", default="",
                    help="指定 mineru-kit 可执行文件（或其所在目录）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="N",
                    help="单文件转换超时秒数，默认 %d" % DEFAULT_TIMEOUT)
    ap.add_argument("--no-postprocess", action="store_true",
                    help="跳过全部后处理，输出 MinerU 原始结果")
    ap.add_argument("--keep-raw", action="store_true",
                    help="保留 MinerU 原始输出目录（调试用）")
    ap.add_argument("--version", action="version", version="pdf2md_mineru %s" % VERSION)
    args = ap.parse_args(argv)

    force_utf8_stdout()

    target = Path(args.target)
    if not target.exists():
        die(EXIT_USAGE, "路径不存在：%s" % target)

    pdfs = collect_pdfs(target)
    if not pdfs:
        if target.is_file():
            die(EXIT_USAGE, "不是 .pdf 文件：%s" % target)
        print("目录内没有 PDF 文件：%s" % target)
        return EXIT_OK

    mineru = find_mineru(args.mineru)
    print("pdf2md_mineru %s | MinerU: %s | 档位: %s | 待处理 %d 个 PDF"
          % (VERSION, mineru, args.tier, len(pdfs)))
    print("-" * 68)

    ok, failures = 0, []
    for pdf in pdfs:
        print("[%s]" % pdf.name)
        try:
            dest, report, err = convert_one(pdf, mineru, args)
        except Exception as e:                      # 单文件异常不中断整批
            err = "%s: %s" % (type(e).__name__, e)
            dest, report = None, None
        if dest is None:
            print("  失败：" + err.replace("\n", "\n        "))
            failures.append((pdf, err))
            print()
            continue
        print("  -> %s" % dest)
        if not args.no_postprocess:
            print_one_report(pdf, report)
        ok += 1
        print()

    print("-" * 68)
    print("成功 %d 个，失败 %d 个，共 %d 个" % (ok, len(failures), len(pdfs)))
    if failures:
        print("失败清单：")
        for pdf, err in failures:
            print("  %s —— %s" % (pdf.name, err.splitlines()[0]))
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
