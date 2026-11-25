#!/usr/bin/env python3
"""根据个人简历，对岗位主表做匹配打分，并为 Top 岗位生成定制招呼语。

输入：parse_resume.py 产出的简历 JSON + 含 JD 的岗位主表 JSON
输出：匹配报告（Markdown/JSON）与可直接用于 auto_greet.py 的 targets.json

打分维度（可解释）：
  技能重合 60% + 薪资匹配 15% + 经验匹配 10% + 学历匹配 10% + Boss 活跃 5%
"""
import argparse
import json
import os
import re

SKILL_WEIGHT = 0.60
SALARY_WEIGHT = 0.15
EXP_WEIGHT = 0.10
EDU_WEIGHT = 0.10
ACTIVE_WEIGHT = 0.05


def split(v):
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def norm_skill(s):
    """技能名归一化，便于 'Golang'/'Go'、'机器学习'/'machine learning' 对齐。"""
    s = s.lower().strip()
    alias = {
        "golang": "go", "c/c++": "c++", "sklearn": "scikit-learn",
        "llm": "大模型", "nlp": "自然语言处理", "k8s": "kubernetes",
        "spark/hadoop/hive": "spark", "大数据处理框架(spark/hadoop/hive)": "spark",
    }
    return alias.get(s, s)


def salary_mid(s):
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*K", str(s or ""))
    return (int(m.group(1)) + int(m.group(2))) / 2 if m else None


def exp_years(s):
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*年", str(s or ""))
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2
    m = re.search(r"(\d+)\s*年", str(s or ""))
    return int(m.group(1)) if m else None


def score_job(job, resume):
    # 归一化用于匹配，原始写法用于展示（保证招呼语里是 Python 而非 python）
    r_map = {norm_skill(s): s for s in resume.get("skills", [])}
    r_skills = set(r_map)
    j_raw = split(job.get("skills")) + split(job.get("skill_tags"))
    j_map = {norm_skill(s): s for s in j_raw}
    j_skills = set(j_map)
    common_norm = sorted(r_skills & j_skills)
    common = [j_map[n] for n in common_norm]  # 岗位原始写法优先
    skill_ratio = len(common_norm) / max(1, len(j_skills))

    # 薪资：用户简历没写期望薪资时不加分也不扣分（中性 0.6）
    mid = salary_mid(job.get("salary"))
    salary_score = 0.6
    expect = resume.get("expected_salary_min")
    if expect and mid:
        salary_score = 1.0 if mid >= float(expect) else max(0.0, mid / float(expect))

    # 经验：简历年限与岗位要求接近得分高
    want = exp_years(job.get("job_labels") or job.get("tags"))
    have = resume.get("years_experience")
    if want and have:
        exp_score = 1.0 if abs(float(have) - want) <= 1 else max(0.1, 1 - abs(float(have) - want) / 5)
    elif want is None:
        exp_score = 0.8
    else:
        exp_score = 0.5

    r_edu = set(resume.get("education", []))
    j_edu = set(split(job.get("job_labels"))) & {"博士", "硕士", "本科", "大专", "学历不限"}
    if not j_edu:
        edu_score = 0.8
    elif not r_edu:
        edu_score = 0.5
    elif "学历不限" in j_edu:
        edu_score = 1.0
    else:
        order = ["博士", "硕士", "本科", "大专"]
        r_level = min((order.index(e) for e in r_edu if e in order), default=3)
        j_level = min((order.index(e) for e in j_edu if e in order), default=3)
        edu_score = 1.0 if r_level <= j_level else max(0.2, 1 - (r_level - j_level) * 0.3)

    active = str(job.get("boss_active_status") or "")
    active_score = 1.0 if "刚刚活跃" in active or "在线" in active else (
        0.7 if "日" in active or "本周" in active else 0.4)

    total = (skill_ratio * SKILL_WEIGHT + salary_score * SALARY_WEIGHT +
             exp_score * EXP_WEIGHT + edu_score * EDU_WEIGHT + active_score * ACTIVE_WEIGHT)
    return round(total * 100, 1), {
        "skill_ratio": round(skill_ratio, 2), "common_skills": common,
        "salary_score": round(salary_score, 2), "exp_score": round(exp_score, 2),
        "edu_score": round(edu_score, 2), "active_score": round(active_score, 2),
    }


def build_greeting(job, resume):
    """用简历里真实存在的技能拼招呼语，不虚构经历。"""
    _, detail = score_job(job, resume)
    common = detail["common_skills"][:4]
    if not common:
        common = resume.get("skills", [])[:4]
    skill_txt = "、".join(common) if common else "相关技术"
    company = job.get("boss_name") or "贵公司"
    title = job.get("title") or "该岗位"
    years = resume.get("years_experience")
    year_txt = f"{int(years)} 年" if years else "多年"
    return (f"您好，看到{company}「{title}」岗位很感兴趣。"
            f"我有{year_txt}相关经验，主要使用{skill_txt}，"
            f"与岗位要求比较契合。方便的话想了解下团队目前的工作重点？")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", required=True, help="parse_resume.py 产出的 JSON")
    ap.add_argument("--jobs", required=True, help="含 JD 的岗位主表 JSON")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out-report", help="输出 Markdown 匹配报告")
    ap.add_argument("--out-targets", help="输出 auto_greet.py 可用的 targets.json")
    ap.add_argument("--greeting-template", help="自定义招呼语模板（可选）")
    args = ap.parse_args()

    with open(args.resume, encoding="utf-8") as f:
        resume = json.load(f)
    with open(args.jobs, encoding="utf-8") as f:
        data = json.load(f)
    jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data

    scored = []
    for i, job in enumerate(jobs):
        s, detail = score_job(job, resume)
        scored.append({"index": i, "score": s, "detail": detail, "job": job})
    scored.sort(key=lambda x: -x["score"])
    top = scored[:args.top]

    report = ["# 简历匹配报告", "",
              f"简历：{os.path.basename(resume.get('source_file', ''))}",
              f"技能：{', '.join(resume.get('skills', [])[:12])}",
              f"经验：{resume.get('years_experience')} 年 · 学历：{'/'.join(resume.get('education', []))}",
              "", f"共匹配 {len(jobs)} 个岗位，Top {len(top)}：", ""]
    targets = []
    for rank, item in enumerate(top, 1):
        job, d, s = item["job"], item["detail"], item["score"]
        greeting = build_greeting(job, resume)
        report += [
            f"## {rank}. {job.get('title')} — {s} 分",
            f"- 公司：{job.get('boss_name')}（{job.get('company_scale')}）",
            f"- 薪资：{job.get('salary')} · {job.get('location')} · {job.get('job_labels')}",
            f"- 技能重合：{', '.join(d['common_skills']) or '无'}（岗位技能覆盖率 {int(d['skill_ratio']*100)}%）",
            f"- 分项：技能 {d['skill_ratio']} / 薪资 {d['salary_score']} / 经验 {d['exp_score']} / 学历 {d['edu_score']} / 活跃 {d['active_score']}",
            f"- 招呼语：{greeting}",
            f"- 链接：{job.get('job_link')}", "",
        ]
        targets.append({
            "job_id": job.get("job_id"),
            "title": job.get("title"),
            "salary": job.get("salary"),
            "score": s,
            "job_url": job.get("job_link"),
            "recipient": job.get("boss_name"),
            "greeting": greeting,
        })
    text = "\n".join(report)

    if args.out_report:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_report)), exist_ok=True)
        with open(args.out_report, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"报告已写入 {args.out_report}")
    else:
        print(text)

    if args.out_targets:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_targets)), exist_ok=True)
        with open(args.out_targets, "w", encoding="utf-8") as f:
            json.dump(targets, f, ensure_ascii=False, indent=2)
        print(f"打招呼目标已写入 {args.out_targets}（{len(targets)} 条）")


if __name__ == "__main__":
    main()
