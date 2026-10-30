"""Conservative exclusions for course administration, not whole source pages."""
import re


_ADMIN_CONCEPT = re.compile(
    r"^(?:课程(?:基本信息|信息|编码|代码|编号|学分|学时|性质|设置|目标|考核|安排|先修)|"
    r"学时分配|考勤(?:要求|规则|制度|管理)|考核(?:方式|占比|安排)|"
    r"先修(?:要求|课程)|授课(?:对象|方式)|实验项目(?:类型|安排|学时))"
)
_ADMIN_QUESTION = re.compile(
    r"(?:本课程|该课程|课程的|教学大纲).{0,100}(?:课程编码|学分|学时|考核方式|考核占比|先修要求|授课方式|考勤规则)"
)


def non_learning_reason(concept, text):
    """Flag clear metadata targets; mentioning a credit field in SQL is allowed."""
    if _ADMIN_CONCEPT.search(concept.strip()) or _ADMIN_QUESTION.search(text):
        return "该条目描述课程管理或教学安排，未提供当前学科的定义、原理、公式或方法。"
    if (re.search(r"(?:过程书写|解题书写|答题格式|考试技巧)", concept + " " + text)
            or re.search(r"(?:作答|答题)前.{0,12}(?:检查什么|先检查|检查哪些)", text)):
        return "该条目偏向应试检查或过程书写要求；默认应考查学科概念、原理、条件或方法，而非背诵作答惯例。"
    return None


def question_context_reason(card):
    """Catch explicit missing-example cues; leave semantic completeness to review.

    No symbol count or formula parser: LaTeX commands and code identifiers are
    not evidence that a question is ambiguous or has multiple learning targets.
    """
    question = card.question
    supplied = re.search(r"下列|以下|如下|给定|给出|候选|```", question)
    if not supplied and re.search(r"(?:讲义|课件|教材)(?:里|中)(?:的)?哪(?:种|个|一)", question):
        return "题目要求回忆讲义里的特定例子，却未给出待判断的例子；请在题干提供内容或改问有依据的知识。"
    if (all(re.search(rf"(?<![A-Za-z0-9_])exp{index}(?![A-Za-z0-9_])", question) for index in (1, 2, 3))
            and re.search(r"(?<![A-Za-z0-9_])for(?![A-Za-z0-9_])", question)
            and not re.search(r"(?<![A-Za-z0-9_])for\s*[(（]", question)):
        return "题目使用exp1、exp2、exp3，却未展示for(exp1; exp2; exp3)结构；请补齐题干上下文。"
    return None


def multiple_target_reason(card):
    """Narrow known overloads; general atomicity is a required semantic review.

    Does not count punctuation, rubric items, or LaTeX symbols. Provided context
    without several requested stages must not block an ordinary algorithm card.
    """
    question = card.question
    if re.search(r"(?:二分|折半)查找", question):
        requests = re.split(r"[？?；;：:]", question)
        requests = [re.sub(r"^(?:给定|已知|设)[^，,]*[，,]", "", part.strip()) for part in requests]
        asked = "；".join(part for part in requests if re.search(r"如何|什么|怎样|分别|是否|怎么|怎样", part))
        stages = [r"初始化|初始", r"循环条件|终止条件|结束条件", r"更新|调整|缩小", r"未找到|没找到|找不到|失败"]
        if sum(bool(re.search(stage, asked)) for stage in stages) >= 3:
            return "二分查找题同时考查多个独立阶段；请保留初始化、循环条件、区间更新或失败判断中的一个核心目标，其余另行制卡。"
    if ("信念" in question and re.search(r"特性|特点|特征", question)
            and re.search(r"高层次|低层次|层次关系", question)
            and re.search(r"关系|决定|统摄", question)):
        return "信念的特性与不同层次信念的关系属于可分别复习的目标；请拆开，不把两者设为同卡必答项。"
    return None


def balanced_points(points, blocks, capacity):
    """Prefer an uncited page before a second point from the same page."""
    pages = {block.block_id: block.page for block in blocks}
    remaining, selected, covered = list(points), [], set()
    while remaining and len(selected) < capacity:
        index = next((i for i, point in enumerate(remaining)
                      if {pages[e.block_id] for e in point.evidence} - covered), 0)
        point = remaining.pop(index)
        selected.append(point)
        covered.update(pages[e.block_id] for e in point.evidence)
    return selected


def unasked_expression_requirement(card):
    """Conservative local gate for an explicit demand to reproduce notation.

    This detects a narrow scope conflict, not semantic quality in general.
    Formula facts without an explicit notation demand remain model-reviewed.
    """
    question = card.question
    if re.search(r"表示|记[法号]|符号|公式|表达式|形式|notation|represent|express|formula|symbol", question, re.I):
        return None
    for point in card.rubric:
        if re.search(r"(?:给出|写出|列出|复述|使用|采用|提供|包含|包括)\s*(?:其|该|对应的|具体的|完整的)?\s*"
                     r"(?:符号表示|符号序列|表示法|数学表示|表示\s*[(（])", point.text):
            return f"评分点{point.point_id}额外要求符号表示，但题目未要求表示法；请移除该要求后重新审核。"
    return None
