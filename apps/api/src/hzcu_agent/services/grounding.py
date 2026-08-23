from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from hzcu_agent.schemas import (
    AgentAnswer,
    AnswerClaim,
    AnswerRevision,
    Evidence,
    VerificationFinding,
)

_URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]\n]+)\]\((<[^>\n]+>|[^)\s\n]+)\)")
_PRIVATE_URL_TARGET = "PRIVATE_URL"
_PRIVATE_URL_TOKEN = "<PRIVATE_URL>"


def _markdown_target(raw_target: str) -> str:
    if raw_target.startswith("<") and raw_target.endswith(">"):
        return raw_target[1:-1]
    return raw_target


def _citation_label_key(value: str) -> str:
    """Reduce citation labels and evidence titles to comparable text."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(
        r"^\s*(?:(?:来源|参见|详见|查看|原文)|source)\s*[:：\-—]*\s*",
        "",
        normalized,
    )
    return "".join(
        character for character in normalized if unicodedata.category(character)[0] in {"L", "N"}
    )


def _title_match_score(label_key: str, title_key: str) -> float:
    if not label_key or not title_key:
        return 0.0
    if label_key == title_key:
        return 1.0
    if label_key in title_key or title_key in label_key:
        overlap = min(len(label_key), len(title_key)) / max(len(label_key), len(title_key))
        return 0.9 + (overlap * 0.1)
    if min(len(label_key), len(title_key)) < 4:
        return 0.0
    matcher = SequenceMatcher(None, label_key, title_key)
    ratio = matcher.ratio()
    label_coverage = sum(block.size for block in matcher.get_matching_blocks()) / len(label_key)
    if label_coverage >= 0.8:
        # A concise citation label may omit a subtitle or acronym from the
        # registered document title while retaining the same ordered words.
        return 0.8 + (label_coverage * 0.15) + (ratio * 0.05)
    return ratio


def _workspace_url_for_label(label: str, evidence: list[Evidence]) -> str | None:
    label_key = _citation_label_key(label)
    if not label_key:
        return None

    scores_by_url: dict[str, float] = {}
    for item in evidence:
        url = item.canonical_url.strip()
        if not url or url in {_PRIVATE_URL_TARGET, _PRIVATE_URL_TOKEN}:
            continue
        aliases = (
            item.title,
            f"{item.publisher} {item.title}",
        )
        score = max(_title_match_score(label_key, _citation_label_key(alias)) for alias in aliases)
        scores_by_url[url] = max(scores_by_url.get(url, 0.0), score)

    ranked = sorted(
        scores_by_url.items(),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked or ranked[0][1] < 0.72:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.08:
        return None
    return ranked[0][0]


def restore_workspace_citation_urls(
    answer: AgentAnswer,
    evidence: list[Evidence],
) -> AgentAnswer:
    """Restore sanitized Markdown targets using only current workspace evidence.

    Some model gateways replace a local, relative canonical URL with
    ``<PRIVATE_URL>``. The visible citation label is matched against evidence
    titles, and a target is restored only when one workspace candidate is a
    clear match. Ambiguous placeholders remain for structural verification.
    """

    changed = False

    def replace_placeholder(match: re.Match[str]) -> str:
        nonlocal changed
        label, raw_target = match.groups()
        if _markdown_target(raw_target) != _PRIVATE_URL_TARGET:
            return match.group(0)
        canonical_url = _workspace_url_for_label(label, evidence)
        if canonical_url is None:
            return match.group(0)
        changed = True
        return f"[{label}](<{canonical_url}>)"

    markdown = _MARKDOWN_LINK_PATTERN.sub(replace_placeholder, answer.answer_markdown)
    if not changed:
        return answer
    return answer.model_copy(update={"answer_markdown": markdown})


@dataclass(frozen=True)
class AppliedRevision:
    answer: AgentAnswer
    findings: list[VerificationFinding]
    protocol_violation: bool


@dataclass(frozen=True)
class PrunedCitations:
    answer: AgentAnswer
    findings: list[VerificationFinding]
    changed: bool


def prune_invalid_citations(
    answer: AgentAnswer,
    evidence: list[Evidence],
) -> PrunedCitations:
    """Deterministic first-stage repair: drop what provably cannot verify.

    Citations pointing outside the task workspace and links to unregistered
    URLs are removed; every claim that still has valid support survives. This
    keeps confirmed facts alive instead of degrading the whole answer.
    """

    evidence_ids = {item.evidence_id for item in evidence}
    evidence_urls = {item.canonical_url.rstrip(".,;，。；") for item in evidence}
    findings: list[VerificationFinding] = []
    changed = False

    claims: list[AnswerClaim] = []
    for claim in answer.claims:
        valid = [citation for citation in claim.citations if citation.evidence_id in evidence_ids]
        if len(valid) != len(claim.citations):
            changed = True
            findings.append(
                VerificationFinding(
                    claim_id=claim.claim_id,
                    severity="warning",
                    code="invalid_evidence_id",
                    message="已删除指向本次证据工作区之外的引用。",
                )
            )
            claim = claim.model_copy(update={"citations": valid})
        claims.append(claim)

    markdown = answer.answer_markdown

    def prune_markdown_link(match: re.Match[str]) -> str:
        nonlocal changed
        label, raw_target = match.groups()
        target = _markdown_target(raw_target)
        is_workspace_url = target.startswith(("http://", "https://", "/"))
        if target != _PRIVATE_URL_TARGET and (
            not is_workspace_url or target.rstrip(".,;，。；") in evidence_urls
        ):
            return match.group(0)
        changed = True
        findings.append(
            VerificationFinding(
                severity="warning",
                code="invalid_url",
                message="已把未绑定本次证据工作区的链接剥离为纯文本。",
            )
        )
        return label

    markdown = _MARKDOWN_LINK_PATTERN.sub(prune_markdown_link, markdown)
    for raw_url in dict.fromkeys(_URL_PATTERN.findall(markdown)):
        if raw_url.rstrip(".,;，。；") in evidence_urls:
            continue
        changed = True
        findings.append(
            VerificationFinding(
                severity="warning",
                code="invalid_url",
                message="已把不属于本次证据工作区的链接剥离为纯文本。",
            )
        )
        markdown = markdown.replace(raw_url, "")
    if _PRIVATE_URL_TOKEN in markdown:
        changed = True
        findings.append(
            VerificationFinding(
                severity="warning",
                code="invalid_url",
                message="已删除未能绑定本次证据工作区的链接占位符。",
            )
        )
        markdown = markdown.replace(_PRIVATE_URL_TOKEN, "")

    if not changed:
        return PrunedCitations(answer=answer, findings=[], changed=False)
    return PrunedCitations(
        answer=answer.model_copy(update={"claims": claims, "answer_markdown": markdown}),
        findings=findings,
        changed=True,
    )


def apply_answer_revision(
    answer: AgentAnswer,
    revision: AnswerRevision,
) -> AppliedRevision:
    """Apply a verifier diff without asking it to restate unchanged claims.

    Editing the claim graph while leaving the prose untouched would let the two
    drift apart, so a claim patch without a rewritten answer_markdown is a
    protocol violation the coordinator degrades on.
    """

    findings: list[VerificationFinding] = []
    claims = list(answer.claims)
    indexes = {claim.claim_id: position for position, claim in enumerate(claims)}

    for patch in revision.claim_patches:
        position = indexes.get(patch.claim_id)
        if patch.action == "remove":
            if position is None:
                findings.append(
                    VerificationFinding(
                        claim_id=patch.claim_id,
                        severity="warning",
                        code="orphan_fact",
                        message="修订补丁要删除的主张不存在，已忽略该补丁。",
                    )
                )
                continue
            claims.pop(position)
        elif patch.action == "replace":
            if patch.claim is None:
                findings.append(
                    VerificationFinding(
                        claim_id=patch.claim_id,
                        severity="error",
                        code="orphan_fact",
                        message="替换补丁没有提供新的主张内容。",
                    )
                )
                continue
            if position is None:
                claims.append(patch.claim)
            else:
                claims[position] = patch.claim
        else:
            if patch.claim is None:
                findings.append(
                    VerificationFinding(
                        claim_id=patch.claim_id,
                        severity="error",
                        code="orphan_fact",
                        message="新增补丁没有提供主张内容。",
                    )
                )
                continue
            if position is None:
                claims.append(patch.claim)
            else:
                claims[position] = patch.claim
        indexes = {claim.claim_id: index for index, claim in enumerate(claims)}

    updates: dict[str, object] = {"claims": claims}
    for name in (
        "headline",
        "answer_markdown",
        "assumptions",
        "next_actions",
        "confidence",
        "verification_mode",
    ):
        value = getattr(revision, name)
        if value is not None:
            updates[name] = value

    violation = bool(revision.claim_patches) and revision.answer_markdown is None
    if violation:
        findings.append(
            VerificationFinding(
                severity="error",
                code="orphan_fact",
                message="修订补丁改动了主张图谱但没有给出修订后的回答正文。",
            )
        )
    return AppliedRevision(
        answer=answer.model_copy(update=updates),
        findings=findings,
        protocol_violation=violation,
    )


@dataclass(frozen=True)
class StructuralGroundingResult:
    findings: list[VerificationFinding]
    citation_coverage: float
    fully_supported_rate: float

    @property
    def requires_semantic_verifier(self) -> bool:
        return any(item.severity in {"warning", "error"} for item in self.findings)

    @property
    def passed(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)


class CitationVerifier:
    """Deterministic protocol checks only; semantic support stays model/human judged."""

    def verify(
        self,
        answer: AgentAnswer,
        evidence: list[Evidence],
    ) -> StructuralGroundingResult:
        evidence_ids = {item.evidence_id for item in evidence}
        evidence_urls = {item.canonical_url.rstrip(".,;，。；") for item in evidence}
        findings: list[VerificationFinding] = []
        seen_claim_ids: set[str] = set()
        campus_claims = [claim for claim in answer.claims if claim.statement_type == "campus_fact"]
        covered_claims = 0
        fully_supported_claims = 0

        for claim in answer.claims:
            if claim.claim_id in seen_claim_ids:
                findings.append(
                    VerificationFinding(
                        claim_id=claim.claim_id,
                        severity="error",
                        code="orphan_fact",
                        message="主张 ID 重复，无法建立稳定引用关系。",
                    )
                )
            seen_claim_ids.add(claim.claim_id)
            valid_supports = [
                citation
                for citation in claim.citations
                if citation.evidence_id in evidence_ids and citation.relation == "supports"
            ]
            for citation in claim.citations:
                if citation.evidence_id not in evidence_ids:
                    findings.append(
                        VerificationFinding(
                            claim_id=claim.claim_id,
                            severity="error",
                            code="invalid_evidence_id",
                            message=f"引用 {citation.evidence_id} 不在本次证据工作区。",
                        )
                    )
            if claim.statement_type != "campus_fact":
                continue
            if valid_supports:
                covered_claims += 1
            else:
                findings.append(
                    VerificationFinding(
                        claim_id=claim.claim_id,
                        severity="error",
                        code="missing_citation",
                        message="校园事实主张没有关联本次调查取得的支持证据。",
                    )
                )
            if (
                claim.support_status == "full"
                and valid_supports
                and all(item.support_status == "full" for item in valid_supports)
            ):
                fully_supported_claims += 1
            else:
                code = {
                    "partial": "partial_support",
                    "contradicted": "contradicted",
                    "stale": "stale",
                }.get(claim.support_status, "unsupported")
                findings.append(
                    VerificationFinding(
                        claim_id=claim.claim_id,
                        severity="warning",
                        code=code,
                        message="该校园事实未被标记为完整、当前且适用的证据支持。",
                    )
                )

        checked_targets: set[str] = set()
        for match in _MARKDOWN_LINK_PATTERN.finditer(answer.answer_markdown):
            target = _markdown_target(match.group(2))
            if (
                not target.startswith(("http://", "https://", "/"))
                and target != _PRIVATE_URL_TARGET
            ):
                continue
            checked_targets.add(target)
            if target == _PRIVATE_URL_TARGET or target.rstrip(".,;，。；") not in evidence_urls:
                findings.append(
                    VerificationFinding(
                        severity="error",
                        code="invalid_url",
                        message="回答包含未绑定本次证据工作区的链接。",
                    )
                )
        for raw_url in _URL_PATTERN.findall(answer.answer_markdown):
            if raw_url in checked_targets:
                continue
            normalized = raw_url.rstrip(".,;，。；")
            if normalized not in evidence_urls:
                findings.append(
                    VerificationFinding(
                        severity="error",
                        code="invalid_url",
                        message="回答包含不属于本次证据工作区的外部链接。",
                    )
                )
        if (
            _PRIVATE_URL_TOKEN in answer.answer_markdown
            and _PRIVATE_URL_TARGET not in checked_targets
        ):
            findings.append(
                VerificationFinding(
                    severity="error",
                    code="invalid_url",
                    message="回答包含未绑定本次证据工作区的链接占位符。",
                )
            )

        denominator = len(campus_claims)
        citation_coverage = covered_claims / denominator if denominator else 1.0
        fully_supported_rate = fully_supported_claims / denominator if denominator else 1.0
        return StructuralGroundingResult(
            findings=findings,
            citation_coverage=citation_coverage,
            fully_supported_rate=fully_supported_rate,
        )
