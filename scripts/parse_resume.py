#!/usr/bin/env python3
"""读取个人简历（PDF/DOCX/TXT/MD），抽取技能、年限、学历、方向等结构化信息。

输出 JSON 供 match_resume.py 使用。所有解析均在本地完成，不上传任何内容。
支持格式：
  - .pdf   （pypdf）
  - .docx  （python-docx，失败时用 zipfile+XML 零依赖降级）
  - .txt/.md（直接读取）
"""
import argparse
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

# 技能词典：与 BOSS 岗位标签对齐，便于匹配
SKILL_DICT = [
    "Python", "Java", "C++", "C/C++", "Golang", "Go", "JavaScript", "TypeScript",
    "Scala", "MATLAB", "R语言", "Shell", "SQL", "MySQL", "Redis", "MongoDB",
    "机器学习", "深度学习", "强化学习", "大模型算法", "大模型", "LLM", "自然语言处理",
    "NLP", "计算机视觉", "图像算法", "语音识别", "多模态算法", "推荐算法", "广告算法",
    "搜索算法", "规控算法", "运筹优化算法", "算法工程化", "模型部署", "模型训练",
    "分布式训练", "Spark", "Hadoop", "Hive", "Flink", "Kafka", "数据仓库",
    "大数据处理", "数据清洗", "特征工程", "AIGC", "Agent", "RAG", "Prompt",
    "PyTorch", "TensorFlow", "Keras", "Scikit-learn", "sklearn", "XGBoost", "LightGBM",
    "Transformer", "BERT", "GPT", "微调", "LoRA", "向量数据库", "OpenCV",
    "Flask", "Django", "FastAPI", "Spring", "Docker", "Kubernetes", "K8s",
    "Linux", "Git", "AWS", "阿里云", "腾讯云", "华为云",
    "产品经理", "项目管理", "团队管理", "数据分析", "数据挖掘", "数据可视化",
    "Tableau", "Power BI", "Excel", "PPT", "Word",
]
EDU_WORDS = ["博士", "硕士", "研究生", "本科", "大专", "专科", "高中", "中专", "学历不限"]
EXP_PATTERNS = [
    r"(\d+(?:\.\d+)?)\s*年(?:以上)?(?:相关|工作|开发|算法|项目|从业)?经验",
    r"工作\s*(\d+)\s*年",
    r"(\d+)\s*年以上",
    r"(\d+)-(\d+)年",
]
SECTION_PATTERNS = [
    ("项目经历", r"项目经历|项目经验|项目经验|项目实践|PROJECTS?"),
    ("工作经历", r"工作经历|工作经验|实习经历| employment|WORK EXPERIENCE"),
    ("教育背景", r"教育背景|教育经历|学历|EDUCATION"),
    ("技能", r"专业技能|技能清单|技能特长|技术栈|SKILLS?"),
    ("自我介绍", r"自我评价|个人简介|自我介绍|关于我|SUMMARY"),
]


def read_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def read_docx(path):
    try:
        from docx import Document
        doc = Document(path)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    except Exception:
        # 零依赖降级：直接解 word/document.xml
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        return re.sub(r"<[^>]+>", "", xml)


def read_plain(path):
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def read_resume(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return read_pdf(path)
    if ext in (".docx", ".doc"):
        if ext == ".doc":
            raise ValueError(".doc 旧格式请先另存为 .docx，或直接导出为 PDF/TXT")
        return read_docx(path)
    if ext in (".txt", ".md", ".markdown", ".text"):
        return read_plain(path)
    raise ValueError(f"不支持的格式 {ext}，请使用 PDF/DOCX/TXT/MD")


def extract_skills(text):
    low = text.lower()
    found, seen = [], set()
    for skill in SKILL_DICT:
        sl = skill.lower()
        if sl in low and sl not in seen:
            found.append(skill)
            seen.add(sl)
    # 长词优先，避免"大模型"与"大模型算法"重复时保留信息量大的
    found.sort(key=lambda s: -len(s))
    return found


def extract_years(text):
    # 优先：简历里明确写出的"N年经验"
    explicit = []
    for pat in EXP_PATTERNS:
        for m in re.finditer(pat, text):
            for g in m.groups():
                try:
                    v = float(g)
                    if 0 < v <= 40:
                        explicit.append(v)
                except (ValueError, TypeError):
                    pass
    if explicit:
        return max(explicit)

    # 回退：从日期区间推断，但排除教育背景段落（避免把读书年限算成工作年限）
    work_text = text
    m = re.search(r"教育背景|教育经历", text)
    if m:
        work_text = text[:m.start()]
    inferred = []
    for m in re.finditer(r"(20\d{2})[./年-]\d{1,2}\s*[-~至到]\s*(20\d{2}|至今|现在)", work_text):
        start = int(m.group(1))
        end = 2026 if m.group(2) in ("至今", "现在") else int(m.group(2))
        if 2000 <= start <= end <= 2027:
            inferred.append(end - start)
    return max(inferred) if inferred else None


def extract_education(text):
    found = []
    for w in EDU_WORDS:
        if w in text and w not in found:
            found.append(w)
    return found


def extract_sections(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    result = {}
    current = None
    for line in lines:
        matched = None
        for name, pat in SECTION_PATTERNS:
            if re.search(pat, line, re.I) and len(line) < 30:
                matched = name
                break
        if matched:
            current = matched
            result.setdefault(current, [])
        elif current and len(result[current]) < 40:
            result[current].append(line)
    return {k: "\n".join(v) for k, v in result.items()}


def extract_target(text):
    """推断求职方向：优先出现频次高的岗位关键词。"""
    targets = ["AI工程师", "算法工程师", "机器学习工程师", "深度学习工程师", "大模型工程师",
               "数据分析师", "数据工程师", "后端工程师", "Java工程师", "前端工程师",
               "产品经理", "运营", "测试工程师", "自动化", "嵌入式", "视觉算法工程师"]
    low = text.lower()
    scored = [(t, low.count(t.lower())) for t in targets]
    scored = [(t, n) for t, n in scored if n > 0]
    scored.sort(key=lambda x: -x[1])
    return [t for t, _ in scored[:3]]


def extract_contact(text):
    phone = re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", text)
    email = re.search(r"[\w.\-]+@[\w.\-]+\.\w+", text)
    return {
        "phone": phone.group(1) if phone else "",
        "email": email.group(0) if email else "",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="简历文件路径（PDF/DOCX/TXT/MD）")
    ap.add_argument("--output", help="输出 JSON 路径；不指定则打印到标准输出")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"文件不存在：{args.input}", file=sys.stderr)
        sys.exit(1)

    text = read_resume(args.input)
    if not text.strip():
        print("简历内容为空（可能是扫描件图片 PDF，需先 OCR）", file=sys.stderr)
        sys.exit(2)

    result = {
        "source_file": os.path.abspath(args.input),
        "char_count": len(text),
        "skills": extract_skills(text),
        "years_experience": extract_years(text),
        "education": extract_education(text),
        "target_roles": extract_target(text),
        "contact": extract_contact(text),
        "sections": extract_sections(text),
        "raw_text": text,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"OK {args.output}")
        print(f"  技能 {len(result['skills'])} 项 / 年限 {result['years_experience']} / "
              f"学历 {result['education']} / 方向 {result['target_roles']}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
