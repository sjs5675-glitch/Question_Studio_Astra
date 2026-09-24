"use strict";

import {
  activeBankSearch, closeBankSearch, createBankRequestSequence, createBankSearchState, filterAndSortBankItems,
  loadBankSearchState, newBankSearch, paginateBankItems, saveBankSearchState,
  selectBankSearch, updateBankSearch, bankGroupIsExpanded, setBankGroupExpanded
} from "./bank-search-state.mjs";
import { createDualViewState, readingAsset, readingMode, resetDualViewState, saveDualViewDraft } from "./saved-dual-view.mjs";

const $ = (id) => document.getElementById(id);

const elements = {
  requestDiagnostics: $("requestDiagnostics"), requestDiagnosticText: $("requestDiagnosticText"),
  createDocument: $("createDocument"), documentEntryPayload: $("documentEntryPayload"),
  skipLink: $("skipLink"), workNav: $("workNav"), savedNav: $("savedNav"), bankNav: $("bankNav"), setsNav: $("setsNav"), learningNav: $("learningNav"),
  workView: $("workView"), savedView: $("savedView"), bankView: $("bankView"), setsView: $("setsView"), learningView: $("learningView"),
  refreshLearning: $("refreshLearning"), learningActive: $("learningActive"),
  learningEligible: $("learningEligible"), learningNeedsReview: $("learningNeedsReview"),
  learningImpacted: $("learningImpacted"), learningBenefitStatus: $("learningBenefitStatus"),
  learningEmpty: $("learningEmpty"), learningList: $("learningList"),
  recoveryProgressPanel: $("recoveryProgressPanel"), recoveryProgressStatus: $("recoveryProgressStatus"),
  recoveryProgressStage: $("recoveryProgressStage"), recoveryProgressItem: $("recoveryProgressItem"),
  recoveryVerificationCount: $("recoveryVerificationCount"), recoveryCropCount: $("recoveryCropCount"),
  recoveryProgressUpdated: $("recoveryProgressUpdated"),
  batchForm: $("batchForm"), sourcePdfFile: $("sourcePdfFile"), solutionPdfFile: $("solutionPdfFile"),
  sourcePdfPath: $("sourcePdfPath"), solutionPdfPath: $("solutionPdfPath"),
  batchPages: $("batchPages"), batchProviderProfile: $("batchProviderProfile"),
  providerProfileFull: $("providerProfileFull"), providerProfileCodexOnly: $("providerProfileCodexOnly"),
  startBatch: $("startBatch"), refreshBatches: $("refreshBatches"),
  batchList: $("batchList"), batchesEmpty: $("batchesEmpty"), batchAnnouncement: $("batchAnnouncement"),
  connection: $("connectionStatus"), importForm: $("importForm"), importPath: $("importPath"),
  importButton: $("importButton"), refreshProjects: $("refreshProjects"), projectList: $("projectList"),
  refreshTrash: $("refreshTrash"), trashList: $("trashList"), trashStatus: $("trashStatus"),
  archiveProject: $("archiveProject"), buildHwpx: $("buildHwpx"), ocrMissing: $("ocrMissing"),
  relinkSolutions: $("relinkSolutions"), solutionRelinkLiveStatus: $("solutionRelinkLiveStatus"),
  tagAllItems: $("tagAllItems"), tagAllProgress: $("tagAllProgress"),
  hwpxBuildResult: $("hwpxBuildResult"), hwpxBuildStatus: $("hwpxBuildStatus"),
  hwpxBuildChecks: $("hwpxBuildChecks"), hwpxDownloadLinks: $("hwpxDownloadLinks"),
  projectsEmpty: $("projectsEmpty"), itemList: $("itemList"), itemsEmpty: $("itemsEmpty"),
  itemCount: $("itemCount"), itemSearch: $("itemSearch"), statusFilter: $("statusFilter"), tagFilter: $("tagFilter"),
  figureStatusFilter: $("figureStatusFilter"),
  coverageRegistered: $("coverageRegistered"), coverageDetailed: $("coverageDetailed"),
  coverageQuickOnly: $("coverageQuickOnly"), coverageReview: $("coverageReview"),
  coverageMissing: $("coverageMissing"), coverageVerified: $("coverageVerified"),
  firstBlockerSummary: $("firstBlockerSummary"),
  welcome: $("welcomePanel"), workspace: $("itemWorkspace"), itemContext: $("itemContext"),
  itemTitle: $("itemTitle"), workflow: $("workflowValue"), revision: $("revisionValue"),
  dirty: $("dirtyValue"), reload: $("reloadItem"), validate: $("validateItem"), save: $("saveItem"),
  solutionLink: $("solutionLinkValue"), solutionLinkDetails: $("solutionLinkDetails"),
  solutionImageFiles: $("solutionImageFiles"), solutionImageDrop: $("solutionImageDrop"),
  solutionImageQueue: $("solutionImageQueue"), applySolutionImages: $("applySolutionImages"),
  solutionImageStatus: $("solutionImageStatus"), revertSolutionImage: $("revertSolutionImage"),
  restoreSolutionContent: $("restoreSolutionContent"), showSolutionCandidates: $("showSolutionCandidates"),
  solutionLinkSummary: $("solutionLinkSummary"), solutionLinkEvidence: $("solutionLinkEvidence"),
  solutionPropagationToggle: $("solutionPropagationToggle"),
  solutionPropagationHelp: $("solutionPropagationHelp"),
  previewSolutionPropagation: $("previewSolutionPropagation"),
  solutionPropagationPreview: $("solutionPropagationPreview"),
  solutionPropagationScope: $("solutionPropagationScope"),
  solutionPropagationCounts: $("solutionPropagationCounts"),
  applySolutionPropagation: $("applySolutionPropagation"),
  solutionFeedbackPanel: $("solutionFeedbackPanel"), solutionFeedbackSource: $("solutionFeedbackSource"),
  solutionFeedbackPart: $("solutionFeedbackPart"), solutionFeedbackAttested: $("solutionFeedbackAttested"),
  solutionFeedbackHelp: $("solutionFeedbackHelp"), solutionFeedbackCorrect: $("solutionFeedbackCorrect"),
  solutionFeedbackReject: $("solutionFeedbackReject"), solutionFeedbackLeft: $("solutionFeedbackLeft"),
  solutionFeedbackTop: $("solutionFeedbackTop"), solutionFeedbackRight: $("solutionFeedbackRight"),
  solutionFeedbackBottom: $("solutionFeedbackBottom"), solutionFeedbackCrop: $("solutionFeedbackCrop"),
  solutionFeedbackHeading: $("solutionFeedbackHeading"), solutionFeedbackSaveHeading: $("solutionFeedbackSaveHeading"),
  solutionFeedbackAliasValue: $("solutionFeedbackAliasValue"), solutionFeedbackAlias: $("solutionFeedbackAlias"),
  solutionFeedbackSync: $("solutionFeedbackSync"),
  questionOcrValue: $("questionOcrValue"), questionOcrSummary: $("questionOcrSummary"),
  explanationOcrValue: $("explanationOcrValue"), explanationOcrSummary: $("explanationOcrSummary"),
  answerSummary: $("answerSummary"),
  tagChips: $("tagChips"), tagReviewBadge: $("tagReviewBadge"), tagEditor: $("tagEditor"),
  tagForm: $("tagForm"), tagSubject: $("tagSubject"), tagUnit: $("tagUnit"),
  tagDifficulty: $("tagDifficulty"), tagProblemTypes: $("tagProblemTypes"), tagKeywords: $("tagKeywords"),
  tagVisualKinds: $("tagVisualKinds"), tagVisualDependency: $("tagVisualDependency"),
  tagHasFigure: $("tagHasFigure"), tagHasGraph: $("tagHasGraph"), tagHasTable: $("tagHasTable"),
  tagHasEquation: $("tagHasEquation"), tagHasBogi: $("tagHasBogi"), tagHasChoices: $("tagHasChoices"),
  autoTagItem: $("autoTagItem"), saveTags: $("saveTags"),
  transcribe: $("transcribeItem"), transcribeProfile: $("transcribeProfile"),
  approve: $("approveItem"), applyCandidate: $("applyCandidate"),
  reviewQueueStatus: $("reviewQueueStatus"), reviewTotal: $("reviewTotal"),
  reviewApproved: $("reviewApproved"), reviewNeeded: $("reviewNeeded"),
  reviewProgress: $("reviewProgress"), nextReview: $("nextReviewItem"), approveNext: $("approveNextItem"),
  tabs: [$("questionTab"), $("explanationTab")], sourceView: $("sourceView"),
  sourceTitle: $("sourceTitle"), editorTitle: $("editorTitle"), previewTitle: $("previewTitle"),
  sourceCanvas: $("sourceCanvas"), sourceImage: $("sourceImage"),
  sourceOverlayLayer: $("sourceOverlayLayer"), overlayToggle: $("overlayToggle"),
  bboxEditStatus: $("bboxEditStatus"), figureReviewPanel: $("figureReviewPanel"),
  figureReviewSummary: $("figureReviewSummary"), figureVariantList: $("figureVariantList"),
  sourcePlaceholder: $("sourcePlaceholder"),
  sourceAssetSelect: $("sourceAssetSelect"), sourceAssetLabel: $("sourceAssetLabel"),
  editor: $("jsonEditor"), editorError: $("editorError"),
  contentViewTab: $("contentViewTab"), jsonViewTab: $("jsonViewTab"),
  contentView: $("ocrContentView"), format: $("formatJson"),
  selectedBlockLabel: $("selectedBlockLabel"), blockEditorFields: $("blockEditorFields"),
  blockEditorEmpty: $("blockEditorEmpty"), answerKindLabel: $("answerKindLabel"),
  answerEditor: $("answerEditor"), answerEditorFields: $("answerEditorFields"),
  preview: $("structurePreview"), candidatePanel: $("candidatePanel"), candidateEmpty: $("candidateEmpty"),
  readingPanels: { question: $("questionReadingPanel"), explanation: $("explanationReadingPanel") },
  candidateCompare: $("candidateCompare"), candidateCurrent: $("candidateCurrent"),
  candidateProposed: $("candidateProposed"), issuesPanel: $("issuesPanel"), issueList: $("issueList"),
  issuesEmpty: $("issuesEmpty"), issueSummary: $("issueSummary"), message: $("appMessage"),
  bankTargetSet: $("bankTargetSet"), createSetFromBank: $("createSetFromBank"), bankSearch: $("bankSearch"), bankNumberSearch: $("bankNumberSearch"), bankSourceFilter: $("bankSourceFilter"),
  bankClassificationToggle: $("bankClassificationToggle"), bankClassification: $("bankClassification"),
  bankSubjectFilter: $("bankSubjectFilter"), bankUnitFilter: $("bankUnitFilter"), bankDifficultyFilter: $("bankDifficultyFilter"),
  bankProblemTypesFilter: $("bankProblemTypesFilter"), bankKeywordsFilter: $("bankKeywordsFilter"), bankFeatureFilter: $("bankFeatureFilter"), bankFigureStatusFilter: $("bankFigureStatusFilter"),
  refreshBank: $("refreshBank"), bankCount: $("bankCount"), bankEmpty: $("bankEmpty"), bankList: $("bankList"),
  bankSearchTabs: $("bankSearchTabs"), newBankSearch: $("newBankSearch"), bankConditionChips: $("bankConditionChips"), resetBankSearch: $("resetBankSearch"),
  bankRange: $("bankRange"), bankSimilarityNotice: $("bankSimilarityNotice"), bankLoadStatus: $("bankLoadStatus"), bankPageSize: $("bankPageSize"), bankSort: $("bankSort"), bankPreviousPage: $("bankPreviousPage"), bankNextPage: $("bankNextPage"), bankPageStatus: $("bankPageStatus"),
  bankSelectionCount: $("bankSelectionCount"), clearBankSelection: $("clearBankSelection"),
  deleteSelectedBankItems: $("deleteSelectedBankItems"), regenerateSelectedFigures: $("regenerateSelectedFigures"),
  selectBankPage: $("selectBankPage"), addSelectedBankItems: $("addSelectedBankItems"), bankTargetSet: $("bankTargetSet"), bankSavedSetLink: $("bankSavedSetLink"), bankSelectionStatus: $("bankSelectionStatus"),
  createQuestionSet: $("createQuestionSet"), setsEmpty: $("setsEmpty"), questionSetList: $("questionSetList"),
  setWorkspace: $("setWorkspace"), setWelcome: $("setWelcome"), setEditor: $("setEditor"), setTitle: $("setTitle"),
  saveQuestionSet: $("saveQuestionSet"), buildQuestionSetHwpx: $("buildQuestionSetHwpx"), setItemCount: $("setItemCount"),
  setItemsEmpty: $("setItemsEmpty"), setItemList: $("setItemList"), setOrderAnnouncement: $("setOrderAnnouncement"),
  setBuildResult: $("setBuildResult"), setBuildStatus: $("setBuildStatus"), setBuildChecks: $("setBuildChecks"), setDownloadLinks: $("setDownloadLinks")
};

const state = {
  batches: [], batchPollTimer: null, batchPollInFlight: false, batchSubmitting: false,
  recoveryPollTimer: null, recoveryPollInFlight: false,
  batchStatuses: new Map(), handledBatchProjects: new Set(), batchHistoryInitialized: false,
  projects: [], items: [], projectId: null, itemId: null, itemSummary: null,
  package: null, envelope: null, registration: null, revision: 0, section: "question", issues: [], tags: null,
  solutionLink: null, solutionPropagation: null, solutionPropagationPreview: null,
  solutionFeedbackSync: null,
  solutionImages: [],
  candidate: null, candidates: [], providerProfiles: {}, figureProfiles: {}, figureProfile: "quality", assetUrls: {},
  assetIdBySection: { question: null, explanation: null },
  dualView: createDualViewState(), dualItemKey: null, lastValidPackage: null,
  editorView: "content", overlaysVisible: true, selectedBlockId: null,
  figureRecropDraft: null,
  dirty: false, busy: false, taggingAll: false, previewTimer: null, messageTimer: null,
  view: "work", bankItems: [], bankBusy: false, bankSelection: new Set(), bankPending: new Set(), questionSets: [], activeSetId: null,
  bankSearch: createBankSearchState(), bankLoadError: null,
  activeSet: null, setBusy: false, setDirty: false, draggedSetIndex: null
};

const BBOX_STEP = 0.005;
const BBOX_FINE_STEP = 0.001;
const BBOX_MIN_SIZE = 0.005;
const APPROVED_STATUSES = new Set(["content_ready", "export_ready"]);
let bankSearchStorageKey = "";
let bankSelectionStorageKey = "";
const bankLoadRequests = createBankRequestSequence();
const bankGroupExpansionByTab = new Map();
let bankReadRequest = 0;
const BANK_GROUP_KEY = "bank-group-expansion";

function saveBankGroupExpansion() {
  try { window.sessionStorage.setItem(BANK_GROUP_KEY, JSON.stringify([...bankGroupExpansionByTab].map(([tab, groups]) => [tab, [...groups]]))); } catch {}
}

function restoreBankGroupExpansion() {
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(BANK_GROUP_KEY) || "[]");
    if (Array.isArray(saved)) for (const [tab, groups] of saved) {
      if (typeof tab === "string" && Array.isArray(groups)) bankGroupExpansionByTab.set(tab, new Set(groups.filter((key) => typeof key === "string")));
    }
  } catch {}
}
const BANK_FILTERS = {
  subject: elements.bankSubjectFilter, unit: elements.bankUnitFilter, source: elements.bankSourceFilter,
  difficulty: elements.bankDifficultyFilter, problemTypes: elements.bankProblemTypesFilter,
  keywords: elements.bankKeywordsFilter, features: elements.bankFeatureFilter, figureStatus: elements.bankFigureStatusFilter
};

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function pipelineNotice(value) {
  const code = String(value?.error_code ?? value?.code ?? value?.error ?? "");
  const text = `${code} ${value?.message ?? value ?? ""}`.toLowerCase();
  if (/figure_provider|figure_generation|figure_budget|그림.*생성.*실패/.test(text)) return { target: "figure", message: "그림을 생성하지 못했습니다. 이미지 서비스 설정을 확인한 뒤 그림 생성만 다시 시도하세요." };
  if (/figure_asset|asset_boundary|그림.*검수/.test(text)) return { target: "figure-review", message: "원본 그림과 선택된 그림을 비교하고 경계·내용을 확인한 뒤 검수 상태를 저장하세요." };
  if (/auth_|login|로그인/.test(text)) return { target: "settings", message: "Astra CLI 로그인이 필요합니다. 이 PC의 Codex 로그인 상태를 확인한 뒤 글자 인식을 다시 시도하세요." };
  if (/storage_|permission|winerror|저장 권한/.test(text)) return { target: "storage", message: "저장소에 기록하지 못했습니다. 편집 내용을 보관하고 저장 공간·파일 권한을 확인하세요. 자동 재시도하지 않습니다." };
  if (/revision_conflict|충돌/.test(text)) return { target: "reload", message: "다른 화면에서 내용이 변경되었습니다. 편집 내용을 보관한 뒤 최신 내용을 다시 불러와 비교하세요." };
  if (/cache_|checkpoint|캐시/.test(text)) return { target: "reload", message: "저장된 처리 결과를 확인할 수 없습니다. 최신 문항을 다시 불러와 상태를 확인하세요. 기존 결과를 덮어쓰지 않았습니다." };
  if (/coverage|crop.*missing|누락|잘림/.test(text)) return { target: "source", message: "원본 영역의 내용이 빠졌을 수 있습니다. 원본 PDF와 잘린 영역을 비교하고 영역을 보완한 뒤 다시 인식하세요." };
  if (/equation|unsupported|수식/.test(text)) return { target: "equation", message: "수식 또는 출력 형식을 확인해야 합니다. 수식 편집에서 현재 표현과 원본을 비교하고 지원되는 표현으로 수정하세요." };
  if (/search_|solution_|해설/.test(text)) return { target: "solution", message: "상세 해설을 아직 확보하지 못했습니다. 해설 원본을 확인한 뒤 답·해설 다시 연결을 실행하세요." };
  if (/timeout|rate_limit|quota|cancel|interrupt/.test(text)) return { target: "text", message: "글자 인식이 완료되지 않았습니다. 사용 한도와 작업 상태를 확인한 뒤 중단된 글자 인식만 다시 시도하세요." };
  if (/cli_|model_|rpc_|output_|astra/.test(text)) return { target: "text", message: "Astra 글자 인식 결과를 적용하지 못했습니다. CLI 연결과 원본을 확인한 뒤 글자 인식만 다시 시도하세요." };
  return { target: "inspect", message: "요청을 완료하지 못했습니다. 입력과 연결 상태를 확인하세요. 자세한 원인은 기술 정보에서 확인할 수 있습니다." };
}

function recordRequestError(value, status) {
  elements.requestDiagnostics.hidden = false;
  elements.requestDiagnosticText.textContent = JSON.stringify({ status, error: value }, null, 2);
}

async function api(path, options = {}) {
  const init = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.headers || {}) }
  };
  if (Object.hasOwn(options, "rawBody")) {
    init.body = options.rawBody;
  } else if (Object.hasOwn(options, "body")) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  let response;
  try { response = await fetch(path, init); }
  catch {
    recordRequestError("network_unavailable", 0);
    throw new ApiError("새 Studio 서버에 연결할 수 없습니다. 실행 상태와 현재 주소를 확인한 뒤 다시 시도하세요.", 0, null);
  }
  const raw = await response.text();
  let body = null;
  if (raw) {
    try { body = JSON.parse(raw); }
    catch {
      recordRequestError("unexpected_non_json_response", response.status);
      throw new ApiError("Studio 응답 형식이 올바르지 않습니다. 새 Studio 주소인지 확인한 뒤 다시 연결하세요.", response.status, null);
    }
  }
  if (!response.ok) {
    const message = body?.message || body?.error || `요청 실패 (${response.status})`;
    recordRequestError(body, response.status);
    const notice = pipelineNotice({ ...body, message });
    throw new ApiError(response.status === 404 ? "요청한 주소를 찾을 수 없습니다. 새 Studio 주소인지 확인한 뒤 화면을 새로고침하세요." : notice.message, response.status, body);
  }
  return body;
}

const endpoint = (...parts) => `/api/${parts.map((part) => encodeURIComponent(String(part))).join("/")}`;

function asArray(payload, key) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.[key])) return payload[key];
  if (Array.isArray(payload?.data?.[key])) return payload.data[key];
  if (Array.isArray(payload?.data)) return payload.data;
  return [];
}

function projectId(project) {
  return String(project?.project_id ?? project?.id ?? project?.name ?? project ?? "");
}

function itemId(item) {
  return String(item?.item_id ?? item?.id ?? item?.unit_id ?? item ?? "");
}

function workflowOf(value) {
  return String(value?.workflow?.status ?? value?.status ?? "unknown");
}

function isRegistered(value) {
  if (typeof value?.registration?.status === "string") {
    return value.registration.status === "registered";
  }
  return workflowOf(value) !== "error";
}

function canConfirmReview(value) {
  const question = value?.content?.question;
  return !APPROVED_STATUSES.has(workflowOf(value))
    && Array.isArray(question?.blocks) && question.blocks.length > 0;
}

function registrationLabel(value) {
  if (value?.registration?.status === "unavailable") return "문제 원본 없음";
  return value?.registration?.content_mode === "structured"
    ? "문제 입력 완료"
    : value?.registration?.source_fallback_available
    ? "문제 원본만 저장"
    : statusLabel(workflowOf(value));
}

function explanationInputLabel(value) {
  const link = value?.solution_link;
  if (link?.transcribed === true) return "해설 타이핑 완료";
  if (link?.transcription_state === "failed") return "해설 타이핑 실패";
  if (link?.transcription_state === "running") return "해설 타이핑 중";
  return link?.source_present ? "해설 원본 있음 · 타이핑 대기" : "해설 원본 없음";
}

function needsAttention(value) {
  const link = value?.solution_link;
  return !link?.source_present || link?.transcription_state === "failed"
    || (link?.transcribed && ["review", "mismatch", "failed"].includes(link?.verification_state));
}

function tagsOf(value) {
  const tags = value?.tags ?? value?.package?.tags ?? value?.classification ?? null;
  return tags && typeof tags === "object" && !Array.isArray(tags) ? tags : null;
}

function tagScalar(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return tagScalar(value.value ?? value.name ?? value.label ?? "");
  }
  return value == null ? "" : String(value).trim();
}

function tagList(value) {
  const source = Array.isArray(value) ? value : typeof value === "string" ? value.split(/[,\n]/) : [];
  return [...new Set(source.map(tagScalar).filter(Boolean))];
}

function tagValues(tags) {
  const source = tags?.values && typeof tags.values === "object" ? tags.values : tags;
  return {
    subject: tagScalar(source?.subject),
    unit: tagScalar(source?.unit ?? source?.unit_name ?? source?.unit_path),
    difficulty: tagScalar(source?.difficulty),
    problemTypes: tagList(source?.problem_types ?? source?.question_types),
    keywords: tagList(source?.keywords ?? source?.concepts),
    features: source?.features && typeof source.features === "object" ? source.features : {}
  };
}

function tagsNeedReview(tags) {
  if (!tags) return true;
  const status = tagScalar(tags?.review_status ?? tags?.review?.status ?? tags?.status).toLowerCase();
  return tags?.needs_review === true
    || ["needs_review", "review_needed", "stale", "failed", "partial"].includes(status);
}

const FEATURE_LABELS = {
  has_figure: "그림", has_graph: "그래프", has_table: "표", has_equation: "수식",
  has_bogi: "보기", has_choices: "선택지"
};
const DIFFICULTY_LABELS = { unknown: "미분류", basic: "쉬움", standard: "보통", advanced: "어려움" };
const FIGURE_STATUS_LABELS = {
  has_regenerated: "재생성 후보 있음", missing_regeneration: "미재생성 그림 있음",
  selected_regenerated: "재생성 그림 사용 중", needs_review: "확인 필요", no_figure: "재생성 대상 없음"
};
const PROBLEM_TYPE_LABELS = {
  concept: "개념 확인", application: "개념 적용", calculation: "계산",
  data_interpretation: "자료 해석", graph_interpretation: "그래프 해석",
  experiment_inquiry: "실험 탐구", condition_judgment: "조건 판단",
  reasoning: "추론", composite: "복합"
};
const VISUAL_KIND_LABELS = {
  diagram: "도식", graph: "그래프", photo: "사진", map: "지도", circuit: "회로",
  microscope: "현미경", table_image: "표 이미지", other: "기타"
};
const SUBJECT_UNITS = {
  과학: ["운동과 에너지", "물질", "생명", "지구와 우주", "과학과 사회", "미분류"],
  통합과학1: ["과학의 기초", "물질과 규칙성", "시스템과 상호작용", "미분류"],
  통합과학2: ["변화와 다양성", "환경과 에너지", "과학과 미래 사회", "미분류"],
  물리학: ["힘과 에너지", "전기와 자기", "빛과 물질", "미분류"],
  "역학과 에너지": ["운동과 힘", "에너지와 열", "시공간과 운동", "미분류"],
  "전자기와 양자": ["전기와 자기", "전자기파", "양자", "미분류"]
};

function featureLabels(features) {
  const labels = Object.entries(FEATURE_LABELS)
    .filter(([key]) => features?.[key] === true)
    .map(([, label]) => label);
  const dependency = tagScalar(features?.visual_dependency);
  if (features?.has_figure === true && dependency === "required") labels.push("그림 필수");
  return [...new Set(labels)];
}

function tagSearchText(value) {
  const tags = tagsOf(value);
  if (!tags) return "";
  const values = tagValues(tags);
  return [values.subject, values.unit, values.difficulty, ...values.problemTypes, ...values.keywords,
    DIFFICULTY_LABELS[values.difficulty],
    ...values.problemTypes.map((value) => PROBLEM_TYPE_LABELS[value]),
    ...featureLabels(values.features), ...tagList(values.features?.visual_kinds),
    ...tagList(values.features?.visual_kinds).map((value) => VISUAL_KIND_LABELS[value])].filter(Boolean).join(" ");
}

function tagSidebarSummary(value) {
  const tags = tagsOf(value);
  if (!tags) return "";
  const values = tagValues(tags);
  return [values.unit, DIFFICULTY_LABELS[values.difficulty] ?? values.difficulty].filter(Boolean).join(" · ");
}

function figureGenerationOf(value) {
  const source = value?.summary && typeof value.summary === "object" ? value.summary : value;
  const summary = source?.figure_generation && typeof source.figure_generation === "object"
    ? source.figure_generation : {};
  const count = (field) => Math.max(0, Number.isFinite(Number(summary[field])) ? Number(summary[field]) : 0);
  const figureCount = count("figure_count");
  const regeneratedCount = Math.min(figureCount, count("regenerated_count"));
  return {
    figureCount,
    regeneratedCount,
    missingRegenerationCount: Math.min(figureCount, count("missing_regeneration_count")),
    selectedRegeneratedCount: Math.min(figureCount, count("selected_regenerated_count")),
    reviewPendingCount: count("review_pending_count"),
    blockedMixedCount: count("blocked_mixed_count")
  };
}

function figureStatusMatches(value, filter) {
  const summary = figureGenerationOf(value);
  if (filter === "has_regenerated") return summary.regeneratedCount > 0;
  if (filter === "missing_regeneration") return summary.missingRegenerationCount > 0;
  if (filter === "selected_regenerated") return summary.selectedRegeneratedCount > 0;
  if (filter === "needs_review") return summary.reviewPendingCount > 0 || summary.blockedMixedCount > 0;
  if (filter === "no_figure") return summary.figureCount === 0;
  return true;
}

function figureGenerationLabel(value) {
  const summary = figureGenerationOf(value);
  return summary.figureCount
    ? `그림 재생성 ${summary.regeneratedCount}/${summary.figureCount}`
    : "재생성 대상 없음";
}

function appendFigureGenerationBadges(container, value) {
  const summary = figureGenerationOf(value);
  const status = textNode("span", figureGenerationLabel(value), "figure-generation-badge");
  status.dataset.status = summary.figureCount ? (summary.missingRegenerationCount ? "partial" : "regenerated") : "none";
  container.append(status);
  if (summary.reviewPendingCount > 0 || summary.blockedMixedCount > 0) {
    const review = textNode("span", "확인 필요", "figure-generation-badge figure-generation-review");
    review.dataset.status = "needs-review";
    container.append(review);
  }
}

function detailFrom(payload) {
  const envelope = payload?.data && typeof payload.data === "object" && !Array.isArray(payload.data)
    ? payload.data : payload;
  const packageData = envelope?.package ?? envelope?.document ?? envelope?.item ??
    (envelope?.item_id ? envelope : null);
  if (!packageData || typeof packageData !== "object" || Array.isArray(packageData)) {
    throw new Error("API 응답에 문항 package가 없습니다.");
  }
  const revision = Number(envelope?.revision ?? packageData.revision ?? packageData.workflow?.revision ?? 0);
  const candidates = envelope?.candidates ?? payload?.candidates ?? [];
  return {
    envelope,
    package: packageData,
    revision: Number.isFinite(revision) ? revision : 0,
    assetUrls: envelope?.asset_urls ?? envelope?.assetUrls ?? payload?.asset_urls ?? {},
    candidate: envelope?.candidate ?? null,
    candidates: Array.isArray(candidates) ? candidates : (candidates ? [candidates] : []),
    registration: envelope?.registration ?? payload?.registration ?? null,
    solutionLink: envelope?.solution_link ?? envelope?.solutionLink ?? payload?.solution_link ?? null,
    solutionPropagation: envelope?.solution_propagation ?? payload?.solution_propagation ?? null,
    tags: envelope?.tags ?? payload?.tags ?? packageData.tags ?? null,
    issues: normalizeIssues(envelope?.issues ?? packageData.review?.issues ?? [])
  };
}

function setConnection(online, label) {
  elements.connection.dataset.state = online ? "online" : "offline";
  elements.connection.textContent = label || (online ? "연결됨" : "연결할 수 없습니다");
}

function notify(message, kind = "info") {
  clearTimeout(state.messageTimer);
  elements.message.textContent = message;
  elements.message.dataset.kind = kind;
  elements.message.dataset.visible = "true";
  state.messageTimer = setTimeout(() => { elements.message.dataset.visible = "false"; }, 4200);
}

function setBusy(busy) {
  state.busy = busy;
  [elements.importButton, elements.refreshProjects, elements.ocrMissing, elements.tagAllItems, elements.reload, elements.transcribeProfile, elements.transcribe, elements.validate, elements.approve, elements.save, elements.applyCandidate, elements.sourceAssetSelect, elements.nextReview, elements.approveNext, elements.autoTagItem, elements.saveTags]
    .forEach((button) => { button.disabled = busy; });
  elements.archiveProject.disabled = busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.buildHwpx.disabled = busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.ocrMissing.disabled = busy || !state.projectId
    || !state.items.some((item) => item?.registration?.content_mode === "source_fallback"
      || (item?.solution_link?.candidate_count > 0 && item?.solution_link?.transcribed !== true));
  elements.relinkSolutions.disabled = busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.tagAllItems.disabled = busy || !state.projectId || state.items.length === 0;
  elements.editor.disabled = busy || Boolean(state.figureRecropDraft);
  elements.format.disabled = busy || Boolean(state.figureRecropDraft);
  setSimpleEditorDisabled(busy || Boolean(state.figureRecropDraft));
  elements.tagForm.querySelectorAll("input, select, button").forEach((control) => { control.disabled = busy; });
  elements.workspace.setAttribute("aria-busy", String(busy));
  elements.workspace.querySelectorAll("[data-edit-section], [data-reading-asset]").forEach((control) => { control.disabled = busy; });
  renderSolutionPropagationControls();
  elements.startBatch.disabled = busy || state.batchSubmitting;
  elements.projectList.querySelectorAll("button.trash-action").forEach((button) => { button.disabled = busy; });
  elements.itemList.querySelectorAll("button.trash-action").forEach((button) => { button.disabled = busy; });
  renderReviewQueue();
  elements.approve.disabled = busy || !canConfirmReview(state.package);
}

function setDirty(dirty) {
  state.dirty = dirty;
  renderUnsavedState();
}

function setFigureRecropDraft(draft) {
  state.figureRecropDraft = draft;
  elements.editor.disabled = state.busy || Boolean(draft);
  elements.format.disabled = state.busy || Boolean(draft);
  setSimpleEditorDisabled(state.busy || Boolean(draft));
  renderUnsavedState();
}

function setSimpleEditorDisabled(disabled) {
  elements.contentView.querySelectorAll("input, textarea, select, button").forEach((control) => {
    control.disabled = disabled;
  });
}

function hasUnsavedWork() {
  return state.dirty || Boolean(state.figureRecropDraft);
}

function requireFinishedRecrop(action) {
  if (!state.figureRecropDraft) return false;
  notify(`그림 영역 수정을 마치거나 취소한 뒤 ${action}해 주세요.`, "error");
  return true;
}

function renderUnsavedState() {
  const unsaved = hasUnsavedWork();
  elements.dirty.textContent = state.dirty ? "저장 안 됨" : state.figureRecropDraft ? "그림 영역 수정 중" : "저장됨";
  elements.dirty.dataset.dirty = String(unsaved);
  const active = $("activeEditSection");
  if (active) active.textContent = `${state.section === "question" ? "문제" : "해설"} 편집 중${unsaved ? " · 저장 안 됨" : ""}`;
}

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(text ?? "");
  return node;
}

function confirmDiscard() {
  return !hasUnsavedWork() || window.confirm("저장하지 않은 변경이 있습니다. 버릴까요?");
}

function batchId(job) {
  return String(job?.job_id ?? job?.id ?? "");
}

function batchState(job) {
  if (job?.status) return String(job.status);
  const raw = String(job?.state ?? "queued");
  return ["queued", "completed", "failed", "interrupted", "cancelled"].includes(raw) ? raw : "running";
}

function batchStage(job) {
  const raw = String(job?.phase ?? job?.stage ?? "preparing");
  return raw.includes("figure") ? "figure_generation" : raw;
}

function applyWorkspaceView() {
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("view");
  const view = ["saved", "bank", "sets", "learning"].includes(requested)
    ? requested
    : (!requested && params.has("project")) ? "saved" : "work";
  state.view = view;
  elements.workView.hidden = view !== "work";
  elements.savedView.hidden = view !== "saved";
  elements.bankView.hidden = view !== "bank";
  elements.setsView.hidden = view !== "sets";
  elements.learningView.hidden = view !== "learning";
  elements.workNav.setAttribute("aria-current", view === "work" ? "page" : "false");
  elements.savedNav.setAttribute("aria-current", view === "saved" ? "page" : "false");
  elements.bankNav.setAttribute("aria-current", view === "bank" ? "page" : "false");
  elements.setsNav.setAttribute("aria-current", view === "sets" ? "page" : "false");
  elements.learningNav.setAttribute("aria-current", view === "learning" ? "page" : "false");
  elements.skipLink.href = ({ work: "#workView", saved: "#mainContent", bank: "#bankView", sets: "#setWorkspace", learning: "#learningView" })[view];
}

function batchStatusLabel(status) {
  return ({
    queued: "대기 중", running: "처리 중", completed: "완료",
    failed: "실패", interrupted: "중단됨", cancelled: "취소됨"
  })[status] || status || "상태 확인 중";
}

function phaseLabel(phase) {
  return ({
    preparing: "준비 중", cropping: "문항 나누는 중", matching: "해설 찾는 중",
    transcribing: "글자 인식 중", validating: "내용 확인 중", finalizing: "마무리 중",
    failed: "실패", interrupted: "중단됨", cancelled: "취소됨", boundary_repair: "경계 교정 실패",
    figure_generation: "그림 처리 중", regenerating: "그림 처리 중", generating_figures: "그림 처리 중"
  })[phase] || phase || "준비";
}

function progressPercent(value) {
  if (value && typeof value === "object") {
    const done = Number(value.completed ?? value.done ?? value.current);
    const total = Number(value.total);
    if (Number.isFinite(done) && Number.isFinite(total) && total > 0) return Math.round(done / total * 100);
  }
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(100, Math.round(number <= 1 ? number * 100 : number)));
}

function batchSummary(job) {
  const summary = job?.summary;
  if (!summary || typeof summary !== "object" || Array.isArray(summary)) return "";
  const saved = Number(summary.registered ?? summary.items ?? summary.item_count ?? 0);
  const recognized = Number(summary.transcribed ?? summary.completed_items ?? 0);
  const failed = Number(summary.failed ?? 0);
  const solution = summary.solution_transcription;
  const targets = Array.isArray(job?.solution_targets) ? job.solution_targets : [];
  if ((solution && typeof solution === "object") || targets.length) {
    const count = (name) => targets.filter((target) => target.state === name).length;
    const found = Number(summary.solution?.linked ?? summary.solution?.candidate_count ?? targets.filter((target) => Boolean(target.source_sha256)).length);
    const done = Number(solution?.applied ?? count("applied")) + Number(solution?.skipped_current ?? count("skipped_current"));
    const retry = Number(solution?.failed ?? count("failed")) + Number(solution?.generated_pending_apply ?? count("generated_pending_apply"));
    const noSource = Number(solution?.no_source ?? count("no_source"));
    const verified = Number(summary.solution_verification?.verified || 0);
    return `후보 찾음 ${found} · 타이핑 완료 ${done} · AI 확인 완료 ${verified} · 수정 필요 ${retry + noSource}`
      + (["completed", "failed"].includes(batchState(job)) && retry + noSource ? ` · 처리 종료 · 재시도 ${retry} · 원본 없음 ${noSource}` : "");
  }
  return [saved ? `저장 ${saved}문항` : "", recognized ? `글자 인식 ${recognized}건` : "", failed ? `미완료 ${failed}건` : ""]
    .filter(Boolean).join(" · ");
}

function batchProviderProfile(job) {
  const profile = String(job?.options?.provider_profile ?? job?.provider_profile ?? job?.settings?.provider_profile ?? "full");
  return profile === "codex_only" ? profile : "full";
}

function batchFigureSummary(job) {
  const figures = job?.summary?.figures;
  if (!figures || typeof figures !== "object" || Array.isArray(figures)) return "";
  const eligible = Number(figures.eligible || 0);
  const completed = Number(figures.materialized ?? figures.succeeded ?? 0);
  const cached = Number(figures.cached || 0);
  const failed = Number(figures.failed || 0);
  const blocked = figures.blocked_counts && typeof figures.blocked_counts === "object"
    ? Object.values(figures.blocked_counts).reduce((total, value) => total + Math.max(0, Number(value) || 0), 0)
    : 0;
  return [eligible ? `그림 대상 ${eligible}개` : "", completed ? `후보 저장 ${completed}개` : "",
    cached ? `캐시 ${cached}개` : "", blocked ? `원본 유지 ${blocked}개` : "", failed ? `실패 ${failed}개` : ""]
    .filter(Boolean).join(" · ");
}

function isFigureError(error) {
  const code = String(error?.error_code ?? error?.code ?? "");
  return String(error?.stage ?? error?.phase ?? error?.section ?? "").includes("figure")
    || code.startsWith("figure_") || Boolean(error?.block_id || error?.figure_id);
}

function isSolutionError(error) {
  const code = String(error?.error_code ?? error?.code ?? "");
  return error?.stage === "solution" || ["solution_link_failed", "solution_relink_failed"].includes(code);
}

function batchErrorText(error) {
  const notice = pipelineNotice(error);
  if (notice.target !== "inspect") return notice.message;
  if (typeof error === "string") return notice.message;
  const code = String(error?.error_code ?? error?.code ?? "");
  if (isFigureError(error)) {
    const reasons = {
      figure_provider_not_configured: "그림 처리 설정 없음",
      provider_not_configured: "그림 처리 설정 없음",
      figure_budget_exceeded: "설정한 비용 상한 초과",
      budget_exceeded: "설정한 비용 상한 초과",
      provider_request_failed: "그림 공급자 요청 실패",
      figure_generation_failed: "AI 그림 생성 실패",
      figure_missing: "원본 그림을 찾지 못함",
      figure_not_eligible: "재생성 대상이 아님"
    };
    const reason = error?.message ?? error?.detail ?? reasons[code] ?? "그림 재생성 실패";
    return String(reason).replace("Gemini API 키", "그림 처리 설정");
  }
  if (isSolutionError(error)) {
    if (code === "solution_relink_failed") {
      return error?.recovery_available === true
        ? "답·해설 재연결 실패 · 기존 저장 내용 보존 · 같은 upstream 결과로 복구 가능"
        : "답·해설 재연결 실패 · 기존 저장 내용 변경 없음";
    }
    const count = Math.max(0, Number(error?.affected_items) || 0);
    return `해설 연결 실패 · 문제${count ? ` ${count}개` : ""}는 저장됨 · 해설 검토 필요`;
  }
  if (error?.unit_id && error?.section && error?.error_code) {
    const section = error.section === "question" ? "문제" : "해설";
    const reasons = {
      provider_not_configured: "설정이 없어 원본을 저장했습니다.",
      revision_conflict: "다른 작업에서 내용이 변경되어 적용하지 않았습니다. 새로고침 후 다시 시도해 주세요.",
      storage_permission_denied: "파일 저장 권한이 없어 적용하지 못했습니다. 권한을 확인한 뒤 다시 시도해 주세요.",
      storage_io_failed: "파일 저장 중 오류가 발생했습니다. 저장 공간과 파일 상태를 확인한 뒤 다시 시도해 주세요."
    };
    const reason = reasons[code] ?? "글자 인식을 완료하지 못해 원본을 저장했습니다.";
    return `${section} · ${reason}`;
  }
  return notice.message;
}

function batchErrors(job) {
  const source = job?.warnings ?? job?.errors;
  const errors = Array.isArray(source) ? source : (source ? [source] : []);
  const figureFailures = Array.isArray(job?.summary?.figures?.failures) ? job.summary.figures.failures : [];
  return [...new Map([...errors, ...figureFailures].map((error) => [JSON.stringify(error), error])).values()]
    .sort((left, right) => Number(isFigureError(right)) - Number(isFigureError(left)));
}

function renderRelinkLiveStatus() {
  const latest = state.batches
    .filter((job) => job?.kind === "solution_relink" && job?.project_id === state.projectId)
    .sort((a, b) => String(b?.created_at ?? "").localeCompare(String(a?.created_at ?? "")))[0];
  const status = latest ? batchState(latest) : "";
  const upstream = latest?.upstream_solution_state;
  const linked = state.items.filter((item) => item?.solution_link?.status === "verified").length;
  const review = state.items.filter((item) => item?.solution_link?.status === "review").length;
  const unfound = state.items.filter((item) => item?.solution_link?.status === "missing").length;
  const message = ["queued", "running"].includes(status) ? "답·해설 결과를 Studio에 반영 중입니다."
    : status === "completed" ? `재연결 작업 완료 · 연결 ${linked} · 검토 ${review} · 미발견 ${unfound}`
    : upstream === "completed" ? "Crop 해설 분석 완료 · Studio 반영 대기"
    : upstream ? "Crop에서 답·해설 검증 계속 진행 중 · 완료 후 Studio 반영 대기" : "";
  elements.solutionRelinkLiveStatus.textContent = message;
  elements.solutionRelinkLiveStatus.hidden = !message;
}

function learningKindLabel(kind) {
  return ({
    correct_pair: "올바른 해설 연결", rejected_pair: "잘못된 해설 연결",
    detail_boundary: "해설 영역", heading_role: "단원 머리말",
    source_number_alias: "문항 번호 대응", answer_cell: "정답표 칸"
  })[kind] || kind;
}

function learningValue(value) {
  return JSON.stringify(value ?? null, null, 2);
}

function renderLearningLibrary(payload) {
  const summary = payload?.summary || {};
  const examples = Array.isArray(payload?.examples) ? payload.examples : [];
  elements.learningActive.textContent = String(Number(summary.active) || 0);
  elements.learningEligible.textContent = String(Number(summary.reviewed_eligible) || 0);
  elements.learningNeedsReview.textContent = String(Number(summary.needs_review) || 0);
  elements.learningImpacted.textContent = String(Number(summary.impacted_uses) || 0);
  elements.learningBenefitStatus.textContent = summary.benefit_status === "cases_available"
    ? "사용자 확인 사례가 있습니다. 실제 분석 개선 효과는 별도 평가 전입니다."
    : "사용자 확인 사례가 없어 분석 개선 효과를 판단하지 않습니다.";
  elements.learningEmpty.hidden = examples.length > 0;
  clearChildren(elements.learningList);
  for (const example of examples) {
    const card = document.createElement("article");
    card.className = "learning-card";
    const heading = document.createElement("div");
    heading.className = "learning-card-heading";
    heading.append(
      textNode("h3", learningKindLabel(example.kind)),
      textNode("span", example.reviewed_eligible ? "참고 가능" : "검토 필요", `learning-status ${example.reviewed_eligible ? "eligible" : "review"}`)
    );
    const source = example.source || {};
    const scope = example.format_scope || {};
    const impact = example.impact || {};
    card.append(
      heading,
      textNode("p", `${source.book || "출처 미상"} · ${String(source.sha256 || "").slice(0, 12)}`, "learning-source"),
      textNode("p", `형식 범위 · PDF ${scope.pdf_page || "?"}쪽 · 문항 ${scope.item_id || "미상"} · ${source.dataset_group || "분류 없음"}`, "learning-scope"),
      textNode("p", `수집·동기화 완료 · 활용 기록 ${Number(impact.total) || 0}건`, "learning-sync")
    );
    const change = document.createElement("details");
    change.className = "learning-change";
    change.append(textNode("summary", "수정 전·후 보기"));
    const comparison = document.createElement("div");
    comparison.className = "learning-comparison";
    const before = document.createElement("pre");
    before.textContent = learningValue(example.correction?.before);
    const after = document.createElement("pre");
    after.textContent = learningValue(example.correction?.after);
    const beforeBlock = document.createElement("div");
    beforeBlock.append(textNode("strong", "수정 전"), before);
    const afterBlock = document.createElement("div");
    afterBlock.append(textNode("strong", "수정 후"), after);
    comparison.append(beforeBlock, afterBlock);
    change.append(comparison);
    const retract = textNode("button", "이 사례 철회", "button learning-retract");
    retract.type = "button";
    retract.addEventListener("click", () => retractLearningExample(example.example_id, retract));
    card.append(change, retract);
    elements.learningList.append(card);
  }
}

async function loadLearningLibrary() {
  elements.refreshLearning.disabled = true;
  try {
    renderLearningLibrary(await api(endpoint("learning", "examples")));
    setConnection(true, "연결됨");
  } catch (error) {
    setConnection(false, "연결할 수 없습니다");
    notify(error.message || "분석 참고 사례를 불러오지 못했습니다.", "error");
  } finally {
    elements.refreshLearning.disabled = false;
  }
}

async function retractLearningExample(exampleId, button) {
  if (!window.confirm("이 사례를 분석 참고 대상에서 철회할까요? 기존 작업 기록은 보존됩니다.")) return;
  button.disabled = true;
  try {
    const result = await api(endpoint("learning", "examples", exampleId, "retract"), {
      method: "POST", body: { reason: "사용자가 Studio에서 분석 참고 사례 철회를 요청함" }
    });
    const affected = result?.affected || {};
    notify(`사례를 철회했습니다. 영향 기록: 프로필 ${Number(affected.profiles) || 0}, 실행 ${Number(affected.runs) || 0}`);
    await loadLearningLibrary();
  } catch (error) {
    notify(error.message || "사례를 철회하지 못했습니다.", "error");
    button.disabled = false;
  }
}

function batchSourceName(job) {
  if (job?.source_name) return job.source_name;
  if (["solution_pipeline", "solution_operation"].includes(job?.kind)) return "해설 자동 타이핑";
  return String(job?.kind || "").includes("figure") ? "선택 문항 그림 재생성" : "문제 PDF";
}

function renderBatches() {
  clearChildren(elements.batchList);
  const batches = [...state.batches]
    .sort((a, b) => String(b?.updated_at ?? "").localeCompare(String(a?.updated_at ?? "")))
    .slice(0, 6);
  const retried = new Set(state.batches.map((job) => String(job?.retry_of || "")).filter(Boolean));
  const figureRetried = new Set(state.batches.map((job) => String(job?.figure_retry_of || "")).filter(Boolean));
  const resumed = new Set(state.batches.map((job) => String(job?.resume_of || "")).filter(Boolean));
  elements.batchesEmpty.hidden = batches.length > 0;

  for (const job of batches) {
    const id = batchId(job);
    const status = batchState(job);
    const upstreamRunning = status === "interrupted" && ["queued", "indexing", "linking"].includes(job?.upstream_solution_state);
    const upstreamDone = status === "interrupted" && job?.upstream_solution_state === "completed";
    const displayStatus = upstreamRunning || upstreamDone ? "running" : status;
    const progress = status === "completed" ? 100 : progressPercent(job?.progress);
    const warnings = batchErrors(job);
    const ocrWarnings = warnings.filter(
      (warning) => !isFigureError(warning) && !isSolutionError(warning)
    );
    const figureFailures = [...new Map(
      (Array.isArray(job?.summary?.figures?.failures) ? job.summary.figures.failures : [])
        .filter((failure) => failure?.item_id && failure?.block_id)
        .map((failure) => [`${failure.item_id}:${failure.block_id}`, failure])
    ).values()];
    const li = document.createElement("li");
    const article = document.createElement("article");
    article.className = "batch-card";
    article.dataset.status = displayStatus;

    const header = document.createElement("div");
    header.className = "batch-card-heading";
    const names = document.createElement("div");
    names.append(textNode("strong", batchSourceName(job), "batch-source"));
    if (job?.solution_name) names.append(textNode("span", `해설: ${job.solution_name}`, "batch-solution"));
    const solutionEnd = status === "completed" && (Number(job?.summary?.solution_transcription?.failed || 0)
      + Number(job?.summary?.solution_transcription?.no_source || 0) > 0);
    const badge = textNode("span", upstreamRunning ? "Crop 처리 중" : upstreamDone ? "Studio 반영 대기" : solutionEnd ? "처리 종료 · 예외 있음" : batchStatusLabel(status), "batch-status");
    badge.dataset.status = displayStatus;
    header.append(names, badge);

    const progressRow = document.createElement("div");
    progressRow.className = "batch-progress-row";
    const phase = textNode("span", upstreamRunning ? "해설 검증 중" : upstreamDone ? "반영 대기" : status === "completed" ? (solutionEnd ? "예외 확인" : "완료") : phaseLabel(batchStage(job)), "batch-phase");
    const progressBar = document.createElement("progress");
    progressBar.max = 100;
    if (!upstreamRunning && !upstreamDone) progressBar.value = progress;
    progressBar.setAttribute("aria-label", upstreamRunning || upstreamDone ? phase.textContent : `${phase.textContent} 진행률`);
    const percent = textNode("span", upstreamRunning || upstreamDone ? "진행 중" : `${progress}%`, "batch-percent");
    progressRow.append(phase, progressBar, percent);

    article.append(header, progressRow);
    const summary = [batchSummary(job), warnings.length ? `확인 내용 ${warnings.length}개` : ""].filter(Boolean).join(" · ");
    article.append(textNode("p", summary, "batch-counts"));
    const providerProfile = batchProviderProfile(job);
    const providerText = providerProfile === "codex_only"
      ? "Astra CLI · 그림 재생성 끔 · 원본\u00a0PDF\u00a0보존"
      : "Astra CLI · 별도 그림 재생성 사용";
    article.append(textNode("p", providerText, "batch-provider-profile"));
    const figureSummary = batchFigureSummary(job);
    if (figureSummary) article.append(textNode("p", figureSummary, "batch-figure-counts"));
    if (upstreamRunning || upstreamDone) article.append(textNode("p", upstreamRunning
      ? "Studio 기록은 중단됐지만 Crop 해설 검증은 계속 진행 중입니다."
      : "Crop 분석이 끝났습니다. Studio 반영을 기다리고 있습니다.", "batch-message"));
    else if (["failed", "interrupted", "cancelled"].includes(status)) article.append(textNode("p", job?.detection_failure && typeof job.message === "string" ? job.message : `${batchStatusLabel(status)} 작업입니다.`, "batch-message"));
    if (job?.updated_at) article.append(textNode("time", new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(new Date(job.updated_at)), "batch-updated"));

    if (warnings.length) {
      const details = document.createElement("details");
      details.className = "batch-errors";
      const summaryNode = document.createElement("summary");
      summaryNode.textContent = `확인 내용 ${warnings.length}개`;
      const list = document.createElement("ul");
      warnings.slice(0, 5).forEach((warning) => list.append(textNode("li", batchErrorText(warning))));
      const technical = document.createElement("details");
      technical.className = "technical-info";
      technical.append(textNode("summary", "기술 정보"), textNode("pre", JSON.stringify(warnings, null, 2)));
      details.append(summaryNode, list, technical);
      article.append(details);
    }

    const blockedRetry = ocrWarnings.some((warning) => ["settings", "storage", "reload"].includes(pipelineNotice(warning).target));
    const retryable = ocrWarnings.length > 0 && !blockedRetry && !retried.has(id) && (
      status === "completed" || (job?.retry_of && ["failed", "interrupted"].includes(status))
    );
    if (retryable) {
      const retry = textNode("button", `실패한 ${ocrWarnings.length}개 다시 인식`, "button batch-retry");
      retry.type = "button";
      retry.addEventListener("click", () => retryBatch(id, retry));
      article.append(retry);
    }

    const figureRetryable = figureFailures.length > 0 && !figureRetried.has(id)
      && ["completed", "failed", "interrupted", "cancelled"].includes(status);
    if (figureRetryable) {
      const retryFigures = textNode(
        "button",
        `실패한 그림 ${figureFailures.length}개 다시 생성`,
        "button batch-figure-retry"
      );
      retryFigures.type = "button";
      retryFigures.addEventListener("click", () => retryFigureBatch(id, retryFigures));
      article.append(retryFigures);
    }

    const cancellable = typeof job?.can_cancel === "boolean"
      ? job.can_cancel
      : ["queued", "running"].includes(status);
    if (cancellable) {
      const cancel = textNode("button", "작업 취소", "button batch-cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => controlBatch(id, "cancel", cancel));
      article.append(cancel);
    }

    if (job?.can_resume === true && !resumed.has(id)) {
      const resume = textNode("button", "중단 지점부터 다시 시작", "button batch-resume");
      resume.type = "button";
      resume.addEventListener("click", () => controlBatch(id, "resume", resume));
      article.append(resume);
    }

    if (status === "completed" && job?.project_id) {
      const open = textNode("a", "저장된 문항에서 열기", "button batch-open");
      open.href = `/?view=saved&project=${encodeURIComponent(String(job.project_id))}`;
      article.append(open);
    }
    li.append(article);
    elements.batchList.append(li);
  }
  renderRelinkLiveStatus();
}

async function retryBatch(id, button) {
  if (!id || button.disabled) return;
  button.disabled = true;
  button.textContent = "재처리 등록 중…";
  try {
    const job = await api(endpoint("batches", id, "retry"), { method: "POST", body: {} });
    state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== batchId(job))];
    state.batchStatuses.set(batchId(job), batchState(job));
    renderBatches();
    notify("원본 등록은 유지하면서 미완료 문제·해설 OCR만 다시 시도합니다.");
    await loadBatches(true);
  } catch (error) {
    button.disabled = false;
    button.textContent = "실패한 내용 다시 인식";
    notify(error.message || "다시 인식을 시작하지 못했습니다.", "error");
  }
}

async function retryFigureBatch(id, button) {
  if (!id || button.disabled) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "그림 재시도 등록 중…";
  try {
    const job = await api(endpoint("batches", id, "retry-figures"), { method: "POST", body: {} });
    state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== batchId(job))];
    state.batchStatuses.set(batchId(job), batchState(job));
    renderBatches();
    notify("성공한 그림과 OCR은 유지하고 실패한 그림만 다시 생성합니다.");
    await loadBatches(true);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    notify(error.message || "그림 재시도를 시작하지 못했습니다.", "error");
  }
}

async function controlBatch(id, action, button) {
  if (!id || button.disabled) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = action === "cancel" ? "취소 요청 중…" : "재개 등록 중…";
  try {
    const job = await api(endpoint("batches", id, action), { method: "POST", body: {} });
    state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== batchId(job))];
    state.batchStatuses.set(batchId(job), batchState(job));
    renderBatches();
    notify(action === "cancel" ? "현재 단계가 끝나는 즉시 안전하게 취소합니다." : "중단된 작업을 이어서 시작했습니다.");
    await loadBatches(true);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    notify(error.message || (action === "cancel" ? "작업을 취소하지 못했습니다." : "작업을 재개하지 못했습니다."), "error");
  }
}

async function refreshProjectListWithoutSelection() {
  const payload = await api(endpoint("projects"));
  state.projects = asArray(payload, "projects");
  renderProjects();
}

async function handleCompletedBatch(job) {
  const id = batchId(job);
  if (!id || !job?.project_id || state.handledBatchProjects.has(id) || state.busy) return;
  if (hasUnsavedWork()) {
    state.handledBatchProjects.add(id);
    await refreshProjectListWithoutSelection();
    notify("일괄 저장이 끝났습니다. 편집 중인 내용 때문에 결과 프로젝트 자동 전환은 보류했습니다.");
    return;
  }
  state.handledBatchProjects.add(id);
  window.location.assign(`/?view=saved&project=${encodeURIComponent(String(job.project_id))}`);
}

async function loadBatches(silent = false) {
  if (state.batchPollInFlight) return;
  state.batchPollInFlight = true;
  try {
    const payload = await api(endpoint("batches"));
    state.batches = asArray(payload, "batches");
    renderBatches();

    const firstLoad = !state.batchHistoryInitialized;
    let completed = null;
    for (const job of state.batches) {
      const id = batchId(job);
      if (!id) continue;
      const status = batchState(job);
      const previous = state.batchStatuses.get(id);
      state.batchStatuses.set(id, status);
      if (firstLoad && status === "completed") state.handledBatchProjects.add(id);
      if (previous && previous !== status && ["completed", "failed", "interrupted", "cancelled"].includes(status)) {
        const announcement = `${job?.source_name || "일괄 작업"}: ${batchStatusLabel(status)}`;
        elements.batchAnnouncement.textContent = announcement;
        if (status !== "completed") notify(announcement, "error");
      }
      if (status === "completed" && job?.project_id && !state.handledBatchProjects.has(id)) {
        if (!completed || String(job?.updated_at ?? "") > String(completed?.updated_at ?? "")) completed = job;
      }
    }
    state.batchHistoryInitialized = true;
    if (completed) await handleCompletedBatch(completed);
  } catch (error) {
    if (!silent) notify(error.message || "일괄 작업 상태를 불러오지 못했습니다.", "error");
  } finally {
    state.batchPollInFlight = false;
  }
}

async function startBatch(event) {
  event.preventDefault();
  if (state.busy || state.batchSubmitting) return;
  const sourcePath = elements.sourcePdfPath.value.trim();
  const solutionPath = elements.solutionPdfPath.value.trim();
  const sourceFile = elements.sourcePdfFile.files?.[0] || null;
  const solutionFile = elements.solutionPdfFile.files?.[0] || null;
  const pages = elements.batchPages.value.trim();
  const providerProfile = elements.providerProfileCodexOnly.checked ? "codex_only" : "full";
  if ((sourceFile && sourcePath) || (solutionFile && solutionPath)) {
    notify("각 PDF는 파일 선택과 서버 경로 중 한 방식만 사용해 주세요.", "error");
    return;
  }
  if ((!sourceFile && !sourcePath.toLocaleLowerCase().endsWith(".pdf"))
      || (!solutionFile && !solutionPath.toLocaleLowerCase().endsWith(".pdf"))) {
    notify("문제와 해설 PDF를 각각 선택해 주세요.", "error");
    return;
  }
  if (!/^(all|\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*)$/i.test(pages)) {
    notify("처리할 쪽은 all, 3-12 또는 3-12,20 형식으로 입력해 주세요.", "error");
    elements.batchPages.focus();
    return;
  }

  state.batchSubmitting = true;
  elements.startBatch.disabled = true;
  elements.startBatch.textContent = sourceFile || solutionFile ? "PDF 업로드 중…" : "작업 등록 중…";
  try {
    const [sourceUpload, solutionUpload] = await Promise.all([
      sourceFile ? uploadPdf(sourceFile) : null,
      solutionFile ? uploadPdf(solutionFile) : null
    ]);
    elements.startBatch.textContent = "작업 등록 중…";
    const requestBody = {
      pages,
      workers: 2,
      crop_dpi: 450,
      ocr_profile: state.providerProfiles.quality_review?.configured ? "quality_review" : "balanced",
      provider_profile: providerProfile,
      regenerate_figures: providerProfile === "full",
      figure_profile: "quality",
      figure_budget_usd: 100,
      remove_blue_text: false
    };
    if (sourceUpload) requestBody.source_upload_id = sourceUpload.upload_id;
    else requestBody.source_path = sourcePath;
    if (solutionUpload) requestBody.solution_upload_id = solutionUpload.upload_id;
    else requestBody.solution_path = solutionPath;
    const payload = await api(endpoint("batches"), {
      method: "POST",
      body: requestBody
    });
    const job = payload?.job ?? payload?.batch ?? payload;
    const id = batchId(job);
    if (id) {
      state.batchStatuses.set(id, batchState(job));
      state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== id)];
      renderBatches();
    }
    elements.sourcePdfFile.value = "";
    elements.solutionPdfFile.value = "";
    notify("일괄 저장 작업을 등록했습니다. 이 화면에서 진행 상황을 확인할 수 있습니다.");
    await loadBatches(true);
  } catch (error) {
    notify(error.message || "일괄 저장 작업을 시작하지 못했습니다.", "error");
  } finally {
    state.batchSubmitting = false;
    elements.startBatch.disabled = state.busy;
    elements.startBatch.textContent = "자동 저장 시작";
  }
}

async function uploadPdf(file) {
  if (!(file instanceof File) || !file.name.toLocaleLowerCase().endsWith(".pdf")) {
    throw new ApiError("PDF 파일만 업로드할 수 있습니다.", 400, null);
  }
  return api(endpoint("uploads", "pdf"), {
    method: "POST",
    headers: {
      "Content-Type": "application/pdf",
      "X-Upload-Name": encodeURIComponent(file.name)
    },
    rawBody: file
  });
}

async function pollBatches() {
  await loadBatches(true);
  state.batchPollTimer = window.setTimeout(pollBatches, state.view === "saved" ? 5000 : 2000);
}

function recoveryItemLabel(value) {
  const match = /^p(\d+)-q(\d+)(?:-sub-(\d+))?$/.exec(String(value || ""));
  if (!match) return "문항 확인 중";
  const subquestion = match[3] ? ` · 소문항 ${Number(match[3])}` : "";
  return `문제 PDF ${Number(match[1])}쪽 · 페이지 내 ${Number(match[2])}번째 문항${subquestion}`;
}

function renderRecoveryProgress(progress) {
  const visible = progress && ["running", "completed", "failed"].includes(progress.status);
  elements.recoveryProgressPanel.hidden = !visible;
  if (!visible) return;
  const statusLabel = { running: "진행 중", completed: "완료", failed: "실패" }[progress.status];
  elements.recoveryProgressStatus.textContent = statusLabel;
  elements.recoveryProgressStatus.dataset.status = progress.status;
  elements.recoveryProgressStage.textContent = progress.stage_text;
  elements.recoveryProgressItem.textContent = `현재 처리 · ${recoveryItemLabel(progress.current_item)}`;
  elements.recoveryVerificationCount.textContent = String(progress.verification_file_count || 0);
  elements.recoveryCropCount.textContent = String(progress.crop_count || 0);
  elements.recoveryProgressUpdated.textContent = `마지막 갱신 ${new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "short", timeStyle: "medium"
  }).format(new Date(progress.updated_at))}`;
}

async function loadRecoveryProgress() {
  if (state.recoveryPollInFlight) return;
  state.recoveryPollInFlight = true;
  try {
    const payload = await api(endpoint("recovery", "progress"));
    renderRecoveryProgress(payload?.progress ?? null);
  } catch {
    renderRecoveryProgress(null);
  } finally {
    state.recoveryPollInFlight = false;
  }
}

async function pollRecoveryProgress() {
  await loadRecoveryProgress();
  state.recoveryPollTimer = window.setTimeout(pollRecoveryProgress, 5000);
}

async function loadProjects(preferredId = null) {
  setBusy(true);
  try {
    const [payload, config] = await Promise.all([
      api(endpoint("projects")),
      api(endpoint("config"))
    ]);
    state.projects = asArray(payload, "projects");
    state.providerProfiles = config?.profiles && typeof config.profiles === "object" ? config.profiles : {};
    state.figureProfiles = config?.figure_profiles && typeof config.figure_profiles === "object" ? config.figure_profiles : {};
    elements.transcribeProfile.value = state.providerProfiles.quality_review?.configured ? "quality_review" : "balanced";
    const hasReadyProvider = Object.values(state.providerProfiles).some((profile) => profile?.configured);
    if (hasReadyProvider) setConnection(true, "연결됨");
    else {
      setConnection(false, "Astra CLI 로그인 확인 필요");
      elements.connection.dataset.state = "warning";
    }
    renderProjects();
    const wanted = preferredId || state.projectId;
    if (wanted && state.projects.some((project) => projectId(project) === String(wanted))) {
      await selectProject(String(wanted), false);
    } else if (state.projects.length === 1) {
      await selectProject(projectId(state.projects[0]), false);
    } else if (!state.projects.length) {
      resetItems("가져온 프로젝트가 없습니다.");
    }
  } catch (error) {
    state.projects = [];
    renderProjects();
    resetItems("연결되면 저장된 문제집이 표시됩니다.");
    setConnection(false, "연결할 수 없습니다");
    notify(error.message || "프로젝트를 불러오지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

function renderProjects() {
  clearChildren(elements.projectList);
  elements.projectsEmpty.hidden = state.projects.length > 0;
  let unnamed = 0;
  for (const project of state.projects) {
    const id = projectId(project);
    if (!id) continue;
    const li = document.createElement("li");
    li.className = "nav-item-with-trash";
    const button = document.createElement("a");
    button.href = `/?view=saved&project=${encodeURIComponent(id)}`;
    button.className = "nav-button";
    button.dataset.projectId = id;
    button.setAttribute("aria-current", String(id === state.projectId));
    const explicitTitle = project?.metadata?.filename_base ?? project?.title ?? project?.name;
    const title = explicitTitle || `이름 없는 문제집 ${++unnamed}`;
    button.append(textNode("span", title, "nav-primary"));
    li.append(button);
    const remove = textNode("button", "제거", "trash-action");
    remove.type = "button";
    remove.setAttribute("aria-label", `${title} 프로젝트를 휴지통으로 이동`);
    remove.disabled = state.busy;
    remove.addEventListener("click", () => moveToTrash("project", id));
    li.append(remove);
    elements.projectList.append(li);
  }
  elements.archiveProject.disabled = state.busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.buildHwpx.disabled = state.busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.ocrMissing.disabled = state.busy || !state.projectId
    || !state.items.some((item) => item?.registration?.content_mode === "source_fallback");
  elements.relinkSolutions.disabled = state.busy || !state.projectId
    || !state.projects.some((project) => projectId(project) === state.projectId);
  elements.tagAllItems.disabled = state.busy || !state.projectId || state.items.length === 0;
  renderRelinkLiveStatus();
}

function resetHwpxBuildResult() {
  elements.hwpxBuildResult.hidden = true;
  elements.hwpxBuildResult.dataset.state = "idle";
  elements.hwpxBuildStatus.textContent = "";
  clearChildren(elements.hwpxBuildChecks);
  clearChildren(elements.hwpxDownloadLinks);
}

function hwpxOutputList(payload) {
  if (Array.isArray(payload?.outputs)) return payload.outputs;
  if (Array.isArray(payload?.receipt?.outputs)) return payload.receipt.outputs;
  return [payload?.question, payload?.explanation].filter((output) => output && typeof output === "object");
}

function hwpxKindLabel(kind) {
  return String(kind || "").toLowerCase() === "explanation" ? "해설" : "문제";
}

function hwpxWarningCodeCounts(warnings) {
  const counts = {};
  for (const warning of warnings) {
    const code = String(warning?.code || "unknown_warning");
    counts[code] = (counts[code] || 0) + 1;
  }
  return counts;
}

function hwpxReceiptSummary(payload) {
  const receipt = payload?.receipt;
  const items = Array.isArray(receipt?.inputs?.items) ? receipt.inputs.items : [];
  const warnings = Array.isArray(receipt?.warnings) ? receipt.warnings : [];
  if (!items.length && !warnings.length) return null;
  const warningCounts = hwpxWarningCodeCounts(warnings);
  return {
    itemCount: items.length,
    sourceCropCount: items.filter((item) => item?.question_render_mode === "source_crop").length,
    answerMissingCount: warningCounts.answer_missing || 0,
    explanationMissingCount: warningCounts.explanation_missing || 0,
    warningCounts
  };
}

function renderHwpxBuildResult(payload) {
  clearChildren(elements.hwpxBuildChecks);
  clearChildren(elements.hwpxDownloadLinks);
  elements.hwpxBuildResult.dataset.state = "success";
  elements.hwpxBuildResult.hidden = false;
  const receiptSummary = hwpxReceiptSummary(payload);
  elements.hwpxBuildStatus.textContent = "문제·해설 HWPX를 만들었습니다.";
  if (receiptSummary) {
    if (receiptSummary.sourceCropCount) elements.hwpxBuildChecks.append(textNode("li", `원본 이미지로 넣은 문제 ${receiptSummary.sourceCropCount}개`));
    if (receiptSummary.answerMissingCount) elements.hwpxBuildChecks.append(textNode("li", `정답 없음 ${receiptSummary.answerMissingCount}개`));
    if (receiptSummary.explanationMissingCount) elements.hwpxBuildChecks.append(textNode("li", `해설 없음 ${receiptSummary.explanationMissingCount}개`));
    const linkChecks = (receiptSummary.warningCounts.solution_link_needs_review || 0) + (receiptSummary.warningCounts.solution_link_missing || 0);
    if (linkChecks) elements.hwpxBuildChecks.append(textNode("li", `해설 연결 확인 필요 ${linkChecks}개`));
  }

  const outputs = hwpxOutputList(payload);
  let downloadCount = 0;
  for (const output of outputs) {
    const label = hwpxKindLabel(output?.document_kind ?? output?.kind);
    const url = output?.download_url;
    if (typeof url !== "string" || !url.startsWith("/api/")) continue;
    const link = textNode("a", `${label} HWPX 받기`, "button");
    link.href = url;
    link.download = String(output?.name || `${state.projectId}-${label}.hwpx`);
    elements.hwpxDownloadLinks.append(link);
    downloadCount += 1;
  }
  if (downloadCount !== 2) throw new Error("문제·해설 다운로드 정보를 모두 받지 못했습니다.");
}

function collectHwpxErrorDetails(value, output = [], depth = 0) {
  if (output.length >= 6 || depth > 4 || value == null) return output;
  if (typeof value === "string") {
    const message = value.trim();
    if (message && !output.includes(message)) output.push(message.slice(0, 400));
    return output;
  }
  if (Array.isArray(value)) {
    value.forEach((child) => collectHwpxErrorDetails(child, output, depth + 1));
    return output;
  }
  if (typeof value === "object") {
    const path = typeof value.path === "string" ? value.path : "";
    const message = typeof value.message === "string" ? value.message : "";
    if (message) collectHwpxErrorDetails(path ? `${path} · ${message}` : message, output, depth + 1);
    ["error", "detail", "errors", "issues"].forEach((key) => {
      if (Object.hasOwn(value, key)) collectHwpxErrorDetails(value[key], output, depth + 1);
    });
  }
  return output;
}

function friendlyHwpxError(error) {
  const details = collectHwpxErrorDetails(error?.body);
  if (error?.message && !details.includes(error.message)) details.unshift(String(error.message).slice(0, 400));
  const joined = details.join(" ").toLowerCase();
  let summary = error?.status === 422
    ? "일부 등록 문항을 원본 또는 저장된 내용으로 처리하지 못했습니다."
    : "HWPX를 만들지 못했습니다.";
  if (/content_ready|export_ready|승인|검수|review/.test(joined)) {
    summary = "등록 상태와 무관한 실제 출력 오류가 남아 있습니다. 아래 항목을 확인해 주세요.";
  } else if (/unsupported|지원하지|nesting|block|table|구조/.test(joined)) {
    summary = "현재 HWPX에서 지원하지 않는 문항 구조가 있습니다. 아래 항목을 확인해 주세요.";
  } else if (/template|profile|템플릿|프로필/.test(joined)) {
    summary = "HWPX 기본 문서 설정을 확인할 수 없습니다.";
  }
  return { summary, details };
}

function renderHwpxBuildError(error) {
  const result = friendlyHwpxError(error);
  clearChildren(elements.hwpxBuildChecks);
  clearChildren(elements.hwpxDownloadLinks);
  elements.hwpxBuildResult.dataset.state = "error";
  elements.hwpxBuildResult.hidden = false;
  elements.hwpxBuildStatus.textContent = result.summary;
  result.details.forEach((detail) => elements.hwpxBuildChecks.append(textNode("li", detail)));
  return result.summary;
}

async function transcribeMissingQuestions() {
  if (state.busy || !state.projectId) return;
  if (hasUnsavedWork()) {
    notify("편집 중인 문항을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const job = await api(endpoint("projects", state.projectId, "ocr", "missing"), {
      method: "POST",
      body: {
        profile: state.providerProfiles.quality_review?.configured ? "quality_review" : "balanced"
      }
    });
    state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== batchId(job))];
    state.batchStatuses.set(batchId(job), batchState(job));
    notify("누락된 문제와 해설을 다시 인식하고 있습니다.");
  } catch (error) {
    notify(error.message || "누락된 내용을 다시 인식하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

function solutionRelinkConfirmation(plan) {
  const counts = plan?.current_status_counts && typeof plan.current_status_counts === "object"
    ? plan.current_status_counts
    : {};
  const providerCalls = plan?.provider_calls_max == null
    ? "산정 불가"
    : `${Number(plan.provider_calls_max) || 0}회 이하`;
  const providerCost = plan?.estimated_cost_usd_max == null
    ? "산정 불가"
    : `$${Number(plan.estimated_cost_usd_max).toFixed(3)} 이하`;
  const cacheStatus = ({
    hit: "캐시 적중",
    miss: "캐시 누락",
    recovery: "완료된 upstream 결과 재적용",
    unknown: "산정 불가"
  })[String(plan?.cache_status || "unknown")] || "산정 불가";
  const lines = [
    `대상 문항 ${Number(plan?.item_count) || 0}개`,
    "기존 문제 crop은 그대로 재사용",
    `현재 완료 ${Number(counts.complete) || 0} · 일부 ${Number(counts.partial) || 0} · 검토 ${Number(counts.review) || 0} · 없음 ${Number(counts.unmatched) || 0} · 충돌 ${Number(counts.conflict) || 0}`,
    `Studio 수동 확정 ${Number(plan?.studio_manual_selection_count) || 0}개 · 승인 문항 ${Number(plan?.studio_approved_item_count) || 0}개는 변경하지 않음`,
    `캐시 상태 ${cacheStatus}`,
    `원격 AI 최대 호출 ${providerCalls}`,
    `예상 비용 ${providerCost}`
  ];
  if (plan?.upstream_already_completed === true) {
    lines.push("이전 upstream 결과를 다시 적용하므로 새 원격 호출은 하지 않습니다.");
  } else if (plan?.provider_calls_possible === true) {
    lines.push("캐시 누락 시 Astra CLI로 필요한 해설만 다시 확인합니다. 그림 생성은 실행하지 않습니다.");
  }
  return `${lines.join("\n")}\n\n이 조건으로 답·해설 전체 재연결을 시작할까요?`;
}

async function relinkProjectSolutions() {
  if (state.busy || !state.projectId) return;
  if (hasUnsavedWork()) {
    notify("편집 중인 문항을 먼저 저장해 주세요.", "error");
    return;
  }
  const requestedProjectId = state.projectId;
  setBusy(true);
  elements.relinkSolutions.textContent = "재연결 조건 확인 중…";
  try {
    const plan = await api(endpoint("projects", requestedProjectId, "solutions", "relink", "preflight"), {
      method: "POST",
      body: {}
    });
    if (!plan?.plan_id || !plan?.plan_sha256) throw new Error("재연결 사전 확인 결과가 올바르지 않습니다.");
    if (!window.confirm(solutionRelinkConfirmation(plan))) {
      notify("답·해설 재연결을 시작하지 않았습니다.");
      return;
    }
    elements.relinkSolutions.textContent = "재연결 작업 등록 중…";
    const job = await api(endpoint("projects", requestedProjectId, "solutions", "relink"), {
      method: "POST",
      body: {
        plan_id: plan.plan_id,
        plan_sha256: plan.plan_sha256,
        confirmed: true
      }
    });
    const id = batchId(job);
    if (!id) throw new Error("재연결 작업 정보를 받지 못했습니다.");
    state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== id)];
    state.batchStatuses.set(id, batchState(job));
    notify("답·해설 전체 재연결 작업을 시작했습니다.");
    window.location.assign("/?view=work");
  } catch (error) {
    notify(error.message || "답·해설 전체 재연결을 시작하지 못했습니다.", "error");
  } finally {
    elements.relinkSolutions.textContent = "답·해설 전체 다시 연결";
    setBusy(false);
    renderProjects();
  }
}

async function buildProjectHwpx() {
  if (state.busy || !state.projectId) return;
  if (hasUnsavedWork()) {
    notify("편집 중인 문항을 먼저 저장한 뒤 HWPX를 만들어 주세요.", "error");
    return;
  }
  const requestedProjectId = state.projectId;
  resetHwpxBuildResult();
  elements.hwpxBuildResult.hidden = false;
  elements.hwpxBuildStatus.textContent = "문제·해설 HWPX를 만드는 중입니다…";
  elements.buildHwpx.textContent = "HWPX 만드는 중…";
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", requestedProjectId, "hwpx", "build"), {
      method: "POST",
      body: {}
    });
    if (state.projectId !== requestedProjectId) return;
    renderHwpxBuildResult(payload);
    notify("문제·해설 HWPX를 만들었습니다.");
  } catch (error) {
    if (state.projectId === requestedProjectId) notify(renderHwpxBuildError(error), "error");
  } finally {
    elements.buildHwpx.textContent = "문제·해설 HWPX 만들기";
    setBusy(false);
    renderProjects();
  }
}

async function exportProjectArchive() {
  if (state.busy || !state.projectId) return;
  if (hasUnsavedWork()) {
    notify("편집 중인 문항을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const result = await api(endpoint("projects", state.projectId, "archive"), {
      method: "POST",
      body: {}
    });
    const downloadUrl = result?.download_url;
    if (typeof downloadUrl !== "string" || !downloadUrl.startsWith("/api/")) {
      throw new Error("백업 파일을 받을 수 없습니다.");
    }
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = String(result?.name || `${state.projectId}.zip`);
    document.body.append(link);
    link.click();
    link.remove();
    notify(result?.cached ? "기존 백업 파일을 받습니다." : "백업 파일을 만들었습니다.");
  } catch (error) {
    notify(error.message || "백업 파일을 만들지 못했습니다.", "error");
  } finally {
    setBusy(false);
    renderProjects();
  }
}

function resetItems(message) {
  state.items = [];
  state.itemId = null;
  state.itemSummary = null;
  renderItems();
  elements.itemsEmpty.textContent = message;
  elements.itemsEmpty.hidden = false;
  closeWorkspace();
}

async function selectProject(id, guard = true) {
  if (state.busy && guard) {
    notify("진행 중인 작업이 끝난 뒤 프로젝트를 바꿔 주세요.");
    return;
  }
  if (guard && id === state.projectId) return;
  if (guard && !confirmDiscard()) return;
  if (state.projectId !== id) resetHwpxBuildResult();
  state.projectId = id;
  renderRelinkLiveStatus();
  state.itemId = null;
  elements.tagAllProgress.hidden = true;
  elements.tagAllProgress.textContent = "";
  renderProjects();
  closeWorkspace();
  elements.itemsEmpty.textContent = "문항을 불러오는 중입니다…";
  elements.itemsEmpty.hidden = false;
  try {
    const payload = await api(endpoint("projects", id, "items"));
    if (state.projectId !== id) return;
    state.items = asArray(payload, "items");
    renderItems();
    if (state.items.length === 1) await selectItem(itemId(state.items[0]), false);
  } catch (error) {
    if (state.projectId !== id) return;
    state.items = [];
    renderItems();
    elements.itemsEmpty.textContent = error.message || "문항을 불러오지 못했습니다.";
    elements.itemsEmpty.hidden = false;
    notify(elements.itemsEmpty.textContent, "error");
  }
}

function filteredItems() {
  const query = elements.itemSearch.value.trim().toLocaleLowerCase("ko-KR");
  const filter = elements.statusFilter.value;
  const tagFilter = elements.tagFilter.value;
  const figureFilter = elements.figureStatusFilter.value;
  return state.items.filter((item) => {
    const id = itemId(item);
    const number = String(item?.identity?.printed_number ?? item?.printed_number ?? "");
    const tags = tagsOf(item);
    const features = tagValues(tags).features;
    const matchesText = !query || `${number} ${id} ${tagSearchText(item)}`.toLocaleLowerCase("ko-KR").includes(query);
    const matchesStatus = filter === "all"
      || (filter === "needs_attention" && needsAttention(item))
      || (filter === "missing_explanation" && item?.solution_link?.transcribed !== true)
      || (filter === "complete" && item?.registration?.content_mode === "structured" && item?.solution_link?.transcribed === true);
    const matchesTag = tagFilter === "all"
      || (tagFilter === "needs_review" && tagsNeedReview(tags))
      || (tagFilter === "figure" && features.has_figure === true)
      || (tagFilter === "graph" && features.has_graph === true);
    return matchesText && matchesStatus && matchesTag && figureStatusMatches(item, figureFilter);
  });
}

const FIRST_BLOCKER_LABELS = {
  crop: "문제 원본 상태 확인 필요",
  anchor: "해설 페이지 참조 없음",
  page_map: "해설 페이지 연결 실패",
  no_candidate: "해설 후보 없음",
  alignment: "문제·해설 대응 실패",
  verification: "해설 후보 검증 실패"
};

function renderSolutionCoverage() {
  const registered = state.items.filter(isRegistered).length;
  const detailed = state.items.filter((item) => item?.solution_link?.source_present === true).length;
  const quickOnly = state.items.filter((item) => item?.solution_link?.quick_answer_only === true).length;
  const review = state.items.filter((item) => item?.solution_link?.review_candidate_present === true).length;
  const missing = state.items.filter((item) => item?.solution_link?.source_present !== true).length;
  const verified = state.items.filter((item) => item?.solution_link?.status === "verified").length;
  elements.coverageRegistered.textContent = String(registered);
  elements.coverageDetailed.textContent = String(detailed);
  elements.coverageQuickOnly.textContent = String(quickOnly);
  elements.coverageReview.textContent = String(review);
  elements.coverageMissing.textContent = String(missing);
  elements.coverageVerified.textContent = String(verified);

  const blockers = new Map();
  state.items.forEach((item) => {
    if (item?.solution_link?.source_present === true) return;
    const code = String(item?.solution_link?.first_blocker || "");
    if (!code) return;
    const label = FIRST_BLOCKER_LABELS[code] || "기타 연결 사유";
    blockers.set(label, (blockers.get(label) || 0) + 1);
  });
  const blockerText = [...blockers.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0], "ko-KR"))
    .map(([label, count]) => `${label} ${count}`)
    .join(" · ");
  elements.firstBlockerSummary.hidden = !blockerText;
  elements.firstBlockerSummary.textContent = blockerText ? `상세 해설 미확보 원인 · ${blockerText}` : "";
}

function renderItems() {
  clearChildren(elements.itemList);
  renderSolutionCoverage();
  const items = filteredItems();
  elements.itemCount.textContent = String(items.length);
  elements.itemsEmpty.hidden = items.length > 0;
  if (!items.length) {
    elements.itemsEmpty.textContent = state.items.length ? "필터에 맞는 문항이 없습니다." :
      (state.projectId ? "이 프로젝트에는 문항이 없습니다." : "프로젝트를 선택하세요.");
  }
  for (const item of items) {
    const id = itemId(item);
    if (!id) continue;
    const number = item?.identity?.printed_number ?? item?.printed_number ?? "–";
    const li = document.createElement("li");
    li.className = "nav-item-with-trash";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "nav-button";
    button.setAttribute("aria-current", String(id === state.itemId));
    const row = document.createElement("span");
    row.className = "item-button-row";
    row.append(textNode("span", number, "item-number"));
    const copy = document.createElement("span");
    copy.className = "item-button-copy";
    copy.append(textNode("span", `${registrationLabel(item)} · ${explanationInputLabel(item)}`, "nav-primary"));
    const tagSummary = tagSidebarSummary(item);
    if (tagSummary) copy.append(textNode("span", tagSummary, "nav-secondary"));
    const figureSummary = document.createElement("span");
    figureSummary.className = "item-figure-summary";
    appendFigureGenerationBadges(figureSummary, item);
    copy.append(figureSummary);
    const linkLabel = solutionLinkDisplayLabel(item?.solution_link, item);
    copy.append(textNode("span", linkLabel, "solution-link-tag"));
    row.append(copy);
    const dot = document.createElement("span");
    dot.className = "status-dot";
    dot.dataset.status = workflowOf(item) === "error" ? "error"
      : needsAttention(item) || tagsNeedReview(tagsOf(item)) || figureStatusMatches(item, "needs_review") ? "warning" : "ready";
    dot.setAttribute("aria-hidden", "true");
    row.append(dot);
    button.append(row);
    button.addEventListener("click", () => selectItem(id));
    li.append(button);
    elements.itemList.append(li);
  }
  elements.ocrMissing.disabled = state.busy || !state.projectId
    || !state.items.some((item) => item?.registration?.content_mode === "source_fallback"
      || (item?.solution_link?.candidate_count > 0 && item?.solution_link?.transcribed !== true));
  elements.tagAllItems.disabled = state.busy || !state.projectId || state.items.length === 0;
  renderReviewQueue();
}

function statusLabel(status) {
  return ({ source_only: "입력 전", transcribed: "입력 완료", needs_review: "저장됨", content_ready: "확인 완료", export_ready: "출력 준비", error: "불러오기 실패" })[status] || status;
}

function solutionLinkLabel(summary) {
  const status = String(summary?.status || "missing");
  if (status === "verified") return "AI 자동 확인됨";
  if (status === "review") return ["review", "mismatch", "failed"].includes(summary?.verification_state) ? "AI 연결 검토 필요" : "AI 연결 확인 전";
  return summary?.source_present ? "AI 연결 확인 전" : "해설 원본 없음";
}

function solutionLinkDisplayLabel(summary, value) {
  if (summary?.manually_confirmed === true) {
    return Number(summary?.confidence || 0) < 0.8 || Number(summary?.review_reason_count || 0) > 0
      ? "저신뢰 · 직접 확인됨"
      : "직접 확인됨";
  }
  return solutionLinkLabel(summary);
}

function nextReviewItemId() {
  if (!state.items.length) return null;
  const currentIndex = state.items.findIndex((item) => itemId(item) === state.itemId);
  for (let offset = 1; offset <= state.items.length; offset += 1) {
    const candidate = state.items[(currentIndex + offset + state.items.length) % state.items.length];
    if (itemId(candidate) !== state.itemId && isRegistered(candidate) && needsAttention(candidate)) return itemId(candidate);
  }
  return null;
}

function renderReviewQueue() {
  const total = state.items.length;
  const questionDone = state.items.filter((item) => item?.registration?.content_mode === "structured").length;
  const explanationDone = state.items.filter((item) => item?.solution_link?.transcribed === true).length;
  const needed = state.items.filter((item) => isRegistered(item) && needsAttention(item)).length;
  elements.reviewTotal.textContent = `${explanationDone}/${total}`;
  elements.reviewApproved.textContent = `${questionDone}/${total}`;
  elements.reviewNeeded.textContent = String(needed);
  elements.reviewProgress.max = Math.max(total, 1);
  elements.reviewProgress.value = explanationDone;
  const percent = total ? Math.round(explanationDone / total * 100) : 0;
  elements.reviewProgress.textContent = `${percent}%`;
  const sources = state.items.filter((item) => item?.solution_link?.source_present).length;
  const verified = state.items.filter((item) => item?.solution_link?.status === "verified").length;
  elements.reviewQueueStatus.textContent = !total
    ? "프로젝트를 선택하세요."
    : `해설 원본 ${sources}/${total} · 타이핑 완료 ${explanationDone}/${total} · AI 확인 완료 ${verified} · 수정 필요 ${needed}`;
  elements.nextReview.disabled = state.busy || !nextReviewItemId();
  elements.approveNext.disabled = state.busy || !canConfirmReview(state.package);
}

async function goToNextReviewItem() {
  if (state.busy) return;
  const nextId = nextReviewItemId();
  if (!nextId) {
    notify("다음 경고 문항이 없습니다.");
    return;
  }
  await selectItem(nextId);
}

function syncCurrentItemSummary() {
  const index = state.items.findIndex((item) => itemId(item) === state.itemId);
  if (index < 0 || !state.package) return;
  state.items[index] = {
    ...state.items[index],
    status: state.package.workflow?.status ?? state.items[index]?.status,
    workflow: state.package.workflow ?? state.items[index]?.workflow,
    registration: state.registration ?? state.items[index]?.registration,
    solution_link: state.solutionLink ?? state.items[index]?.solution_link,
    figure_generation: state.envelope?.figure_generation ?? state.items[index]?.figure_generation,
    tags: state.tags ?? state.items[index]?.tags,
    warning_count: state.registration?.warning_count ?? state.items[index]?.warning_count,
    issue_count: state.issues.length
  };
  state.itemSummary = state.items[index];
  renderItems();
}

async function selectItem(id, guard = true) {
  if (state.busy && guard) {
    notify("진행 중인 작업이 끝난 뒤 문항을 바꿔 주세요.");
    return;
  }
  if (guard && id === state.itemId) return;
  if (guard && !confirmDiscard()) return;
  state.itemId = id;
  state.itemSummary = state.items.find((item) => itemId(item) === id) ?? null;
  closeWorkspace();
  renderItems();
  await loadItem();
}

async function loadItem() {
  if (!state.projectId || !state.itemId) return;
  const requestKey = `${state.projectId}/${state.itemId}`;
  setBusy(true);
  elements.editorError.textContent = "";
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId));
    if (`${state.projectId}/${state.itemId}` === requestKey) {
      applyDetail(detailFrom(payload));
    }
  } catch (error) {
    notify(error.message || "문항을 불러오지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

function applyDetail(detail) {
  clearSolutionImages();
  const detailKey = `${state.projectId}/${state.itemId}`;
  const newItem = state.dualItemKey !== detailKey;
  const previousSourceId = state.package?.provenance?.solution_selection?.source_asset_id;
  if (newItem) {
    resetDualViewState(state.dualView);
    state.dualItemKey = detailKey;
  }
  state.envelope = detail.envelope;
  state.package = detail.package;
  state.lastValidPackage = detail.package;
  const selectedSourceId = detail.package?.provenance?.solution_selection?.source_asset_id;
  if (selectedSourceId && previousSourceId && selectedSourceId !== previousSourceId && !newItem) {
    state.dualView.explanation.assetId = selectedSourceId;
    state.assetIdBySection.explanation = selectedSourceId;
  }
  if (newItem) {
    for (const section of ["question", "explanation"]) {
      const view = state.dualView[section];
      const hasContent = Boolean(detail.package?.content?.[section]?.blocks?.length);
      if ((view.mode === "source" && !sourceAssets(section).length) || (view.mode === "read" && !hasContent)) view.mode = null;
    }
  }
  state.registration = detail.registration;
  state.solutionLink = detail.solutionLink;
  state.solutionPropagation = detail.solutionPropagation;
  state.solutionPropagationPreview = null;
  state.solutionFeedbackSync = null;
  elements.solutionPropagationToggle.checked = false;
  state.tags = detail.tags;
  state.revision = detail.revision;
  state.solutionFeedbackSync = savedSolutionFeedbackSync();
  state.assetUrls = detail.assetUrls && typeof detail.assetUrls === "object" ? detail.assetUrls : {};
  state.candidate = detail.candidate;
  state.candidates = detail.candidates;
  state.issues = detail.issues;
  state.selectedBlockId = firstBlockId(state.package, state.section);
  setFigureRecropDraft(null);
  elements.bboxEditStatus.textContent = "";
  elements.editor.value = JSON.stringify(state.package, null, 2);
  state.editorView = "content";
  setDirty(false);
  elements.welcome.hidden = true;
  elements.workspace.hidden = false;
  renderHeader();
  renderTags();
  renderSolutionLinkDetails();
  const canRevertSource = Number(state.package?.provenance?.solution_selection?.revision || 0) > 1;
  elements.revertSolutionImage.disabled = !canRevertSource;
  elements.restoreSolutionContent.disabled = !canRevertSource;
  renderEditorView();
  renderSource();
  renderPreview(state.package);
  renderDualReading();
  renderCandidate();
  renderIssues();
  syncCurrentItemSummary();
  renderReviewQueue();
}

function closeWorkspace() {
  state.package = null;
  state.lastValidPackage = null;
  state.envelope = null;
  state.registration = null;
  state.solutionLink = null;
  state.solutionPropagation = null;
  state.solutionPropagationPreview = null;
  state.solutionFeedbackSync = null;
  state.tags = null;
  state.assetUrls = {};
  state.assetIdBySection = { question: null, explanation: null };
  state.candidate = null;
  state.candidates = [];
  state.issues = [];
  state.selectedBlockId = null;
  setFigureRecropDraft(null);
  elements.bboxEditStatus.textContent = "";
  setDirty(false);
  elements.workspace.hidden = true;
  elements.welcome.hidden = false;
  renderReviewQueue();
}

function inlinePlainText(inlines) {
  return (Array.isArray(inlines) ? inlines : []).map((inline) => {
    if (inline?.type === "line_break") return "\n";
    return inline?.text ?? inline?.source_text ?? inline?.latex ?? "";
  }).join("").trim();
}

function sectionStats(packageData, section) {
  const blocks = packageData?.content?.[section]?.blocks;
  const result = { blocks: Array.isArray(blocks) ? blocks.length : 0, figures: 0, tables: 0, equations: 0 };
  walkBlocks(blocks, (block) => {
    if (block.type === "figure") result.figures += 1;
    if (block.type === "table") result.tables += 1;
    if (block.type === "paragraph") result.equations += (block.inlines || []).filter((inline) => inline?.type === "equation").length;
  });
  return result;
}

function sectionStatsText(stats) {
  const parts = [`내용 ${stats.blocks}개`];
  if (stats.figures) parts.push(`그림 ${stats.figures}`);
  if (stats.tables) parts.push(`표 ${stats.tables}`);
  if (stats.equations) parts.push(`수식 ${stats.equations}`);
  return parts.join(" · ");
}

function answerSummaryText(packageData) {
  const answer = packageData?.content?.answer;
  if (!answer) return "없음";
  if (answer.kind === "choice") {
    const labels = new Map(questionChoices(packageData).map((choice) => [choice.id, choice.label]));
    return (answer.choice_ids || []).map((id) => labels.get(id) || "선택지").join(", ") || "선택됨";
  }
  if (answer.kind === "true_false") return `${answer.items?.length || 0}개 진술`;
  const texts = [];
  walkAnswerBlocks(answer, (block) => {
    if (block.type === "paragraph") texts.push(inlinePlainText(block.inlines));
  });
  const text = texts.filter(Boolean).join(" ");
  return text ? `${text.slice(0, 80)}${text.length > 80 ? "…" : ""}` : "저장됨";
}

function renderSectionLabels() {
  const label = state.section === "question" ? "문제" : "해설";
  elements.sourceTitle.textContent = `${label} 원본`;
  elements.editorTitle.textContent = `${label} 타이핑 결과`;
  elements.previewTitle.textContent = `${label} 미리보기`;
  elements.transcribe.textContent = `${label} 다시 인식`;
  elements.answerEditor.hidden = state.section !== "explanation";
}

function displayDifficulty(value) {
  return DIFFICULTY_LABELS[value] ?? value;
}

function appendTagChip(text, kind = "default") {
  if (!text) return;
  const chip = textNode("span", text, "tag-chip");
  chip.dataset.kind = kind;
  elements.tagChips.append(chip);
}

function renderTagUnitOptions(subject, selected = "") {
  const units = SUBJECT_UNITS[subject] ?? ["미분류"];
  elements.tagUnit.replaceChildren(...units.map((unit) => {
    const option = document.createElement("option");
    option.value = unit;
    option.textContent = unit;
    return option;
  }));
  elements.tagUnit.value = units.includes(selected) ? selected : "미분류";
}

function renderTags() {
  const tags = state.tags ?? tagsOf(state.package);
  const values = tagValues(tags);
  clearChildren(elements.tagChips);
  appendTagChip(values.subject, "subject");
  appendTagChip(values.unit, "unit");
  appendTagChip(displayDifficulty(values.difficulty), "difficulty");
  values.problemTypes.forEach((value) => appendTagChip(PROBLEM_TYPE_LABELS[value] ?? value, "type"));
  featureLabels(values.features).forEach((value) => appendTagChip(value, "feature"));
  values.keywords.forEach((value) => appendTagChip(value, "keyword"));
  if (!elements.tagChips.childElementCount) {
    elements.tagChips.append(textNode("span", "아직 태그가 없습니다.", "tag-empty"));
  }
  elements.tagReviewBadge.hidden = !tagsNeedReview(tags);

  elements.tagSubject.value = values.subject;
  renderTagUnitOptions(values.subject, values.unit);
  elements.tagDifficulty.value = Object.hasOwn(DIFFICULTY_LABELS, values.difficulty) ? values.difficulty : "unknown";
  elements.tagProblemTypes.value = values.problemTypes.map((value) => PROBLEM_TYPE_LABELS[value] ?? value).join(", ");
  elements.tagKeywords.value = values.keywords.join(", ");
  elements.tagVisualKinds.value = tagList(values.features.visual_kinds).map((value) => VISUAL_KIND_LABELS[value] ?? value).join(", ");
  elements.tagVisualDependency.value = ["none", "supplementary", "required", "unknown"].includes(values.features.visual_dependency)
    ? values.features.visual_dependency : "unknown";
  elements.tagHasFigure.checked = values.features.has_figure === true;
  elements.tagHasGraph.checked = values.features.has_graph === true;
  elements.tagHasGraph.indeterminate = values.features.has_graph == null;
  elements.tagHasTable.checked = values.features.has_table === true;
  elements.tagHasEquation.checked = values.features.has_equation === true;
  elements.tagHasBogi.checked = values.features.has_bogi === true;
  elements.tagHasChoices.checked = values.features.has_choices === true;
}

function splitTagInput(value) {
  return [...new Set(String(value || "").split(/[,\n]/).map((item) => item.trim()).filter(Boolean))];
}

function mappedTagInput(value, labels, fieldLabel) {
  const reverse = new Map(Object.entries(labels).flatMap(([id, label]) => [[id.toLocaleLowerCase(), id], [label.toLocaleLowerCase("ko-KR"), id]]));
  const result = [];
  const invalid = [];
  for (const raw of splitTagInput(value)) {
    const matched = reverse.get(raw.toLocaleLowerCase("ko-KR"));
    if (matched) result.push(matched);
    else invalid.push(raw);
  }
  if (invalid.length) throw new Error(`${fieldLabel}에 사용할 수 없는 값이 있습니다: ${invalid.join(", ")}`);
  return [...new Set(result)];
}

function editedTags() {
  const current = state.tags && typeof state.tags === "object" ? structuredClone(state.tags) : {};
  const target = current.values && typeof current.values === "object" ? current.values : current;
  const previous = tagValues(current);
  target.subject = elements.tagSubject.value;
  target.unit = elements.tagUnit.value.trim();
  if (!target.subject || !target.unit) throw new Error("과목과 단원을 입력해 주세요.");
  target.difficulty = elements.tagDifficulty.value;
  target.problem_types = mappedTagInput(elements.tagProblemTypes.value, PROBLEM_TYPE_LABELS, "문제 유형");
  target.keywords = splitTagInput(elements.tagKeywords.value);
  let visualKinds = mappedTagInput(elements.tagVisualKinds.value, VISUAL_KIND_LABELS, "그림 종류");
  let hasGraph = elements.tagHasGraph.indeterminate ? null : elements.tagHasGraph.checked;
  let hasFigure = elements.tagHasFigure.checked || visualKinds.length > 0 || hasGraph === true;
  if (hasGraph === true && !visualKinds.includes("graph")) visualKinds.push("graph");
  if (hasGraph === false) visualKinds = visualKinds.filter((value) => value !== "graph");
  if (!hasFigure) {
    hasGraph = false;
    visualKinds = [];
  }
  target.features = {
    ...previous.features,
    has_figure: hasFigure,
    has_graph: hasGraph,
    has_table: elements.tagHasTable.checked,
    has_equation: elements.tagHasEquation.checked,
    has_bogi: elements.tagHasBogi.checked,
    has_choices: elements.tagHasChoices.checked,
    visual_kinds: visualKinds,
    visual_dependency: hasFigure ? elements.tagVisualDependency.value : "none"
  };
  return current;
}

function renderHeader() {
  const number = state.package?.identity?.printed_number ?? state.itemSummary?.printed_number ?? "";
  const project = state.projects.find((value) => projectId(value) === state.projectId);
  elements.itemContext.textContent = project?.metadata?.filename_base ?? project?.title ?? project?.name ?? state.projectId ?? "";
  elements.itemTitle.textContent = number ? `${number}번` : "문항";
  elements.workflow.textContent = registrationLabel({
    workflow: state.package?.workflow,
    registration: state.registration
  });
  const questionStats = sectionStats(state.package, "question");
  const explanationStats = sectionStats(state.package, "explanation");
  elements.questionOcrValue.textContent = questionStats.blocks ? "입력 완료" : "입력 안 됨";
  elements.questionOcrSummary.textContent = sectionStatsText(questionStats);
  elements.explanationOcrValue.textContent = explanationInputLabel({ solution_link: state.solutionLink });
  elements.explanationOcrSummary.textContent = sectionStatsText(explanationStats);
  elements.answerSummary.textContent = answerSummaryText(state.package);
  elements.solutionLink.textContent = solutionLinkDisplayLabel(state.solutionLink, state.package);
  elements.solutionLink.dataset.status = String(state.solutionLink?.status || "missing");
  elements.revision.textContent = String(state.revision);
  elements.approve.disabled = state.busy || !canConfirmReview(state.package);
  elements.approve.textContent = APPROVED_STATUSES.has(workflowOf(state.package)) ? "확인 완료됨" : "이대로 확인하고 다음";
  renderSectionLabels();
}

function renderSolutionLinkDetails() {
  clearChildren(elements.solutionLinkEvidence);
  const links = Array.isArray(state.package?.provenance?.solution_links)
    ? state.package.provenance.solution_links.filter((link) => link?.kind === "detailed_solution" || link?.kind === "mixed")
    : [];
  const rawLink = state.package?.provenance?.raw_snapshot?.link_item;
  const selected = links.slice().sort((left, right) => {
    const priority = { verified: 0, review: 1, missing: 2 };
    const statusOrder = (priority[left?.status] ?? 3) - (priority[right?.status] ?? 3);
    return statusOrder || Number(right?.confidence || 0) - Number(left?.confidence || 0);
  })[0] || null;
  const quickAnswer = rawLink?.quick_answer_agreement;
  const confidence = Number(state.solutionLink?.confidence ?? selected?.confidence ?? 0);
  const status = String(state.solutionLink?.status || "missing");
  const descriptions = {
    verified: "문항 번호와 정답을 비교해 같은 문항의 해설로 연결했습니다.",
    review: ["review", "mismatch", "failed"].includes(state.solutionLink?.verification_state)
      ? "AI 연결 검토가 필요합니다. 저장된 타이핑은 별도로 사용할 수 있습니다."
      : "AI 연결 확인 전입니다. 저장된 타이핑은 별도로 사용할 수 있습니다.",
    missing: "AI 연결 확인 전입니다. 후보·출처 정보를 확인하세요."
  };
  elements.solutionLinkSummary.textContent = `${explanationInputLabel({ solution_link: state.solutionLink })} · ${solutionLinkDisplayLabel(state.solutionLink, state.package)}${status === "verified" && confidence > 0 ? ` · 신뢰도 ${Math.round(Math.min(1, confidence) * 100)}%` : ""}`;
  const reasonLabels = {
    problem_crop_unverified: "문제 원본의 일부가 잘렸거나 선명하지 않습니다.",
    best_effort_exhausted: "사용 가능한 자동 비교를 모두 마쳤습니다.",
    no_candidate: "같은 문항 번호의 상세 해설을 찾지 못했습니다.",
    anchor_unresolved: "해설의 위치를 정확히 확정하지 못했습니다.",
    cross_document_verification_pending: "문제와 해설의 내용을 한 번 더 비교하는 것이 좋습니다.",
    independent_verification_inconclusive: "자동 확인 결과가 불확실합니다.",
    review_candidate_asset_missing: "검토 후보는 기록됐지만 후보 이미지를 저장하지 못했습니다. 문제는 정상 저장되며 해설만 확인이 필요합니다."
  };
  const rawReasons = [
    ...(Array.isArray(selected?.review_reasons) ? selected.review_reasons : []),
    ...(!selected && Array.isArray(rawLink?.review_reasons) ? rawLink.review_reasons : [])
  ];
  const evidence = [state.solutionLink?.manually_confirmed
    ? "직접 확인한 해설입니다. 자동 연결 신뢰도 기록은 그대로 보존합니다."
    : descriptions[status]];
  for (const reason of rawReasons) {
    const key = Object.keys(reasonLabels).find((candidate) => String(reason).includes(candidate));
    if (key) evidence.push(reasonLabels[key]);
    else if (String(reason).includes("fingerprint")) evidence.push("문제와 해설의 핵심 문구 일치도가 낮습니다.");
  }
  if (quickAnswer?.answer_text) evidence.push(`빠른 정답 ${quickAnswer.answer_text} · 일치도 ${Math.round(Math.max(0, Math.min(1, Number(quickAnswer.confidence || 0))) * 100)}%`);
  const attempts = Number(state.solutionLink?.attempt_count || 0);
  if (attempts) evidence.push(`자동 비교 ${attempts}단계를 완료했습니다.`);
  [...new Set(evidence.filter(Boolean))].forEach((value) => elements.solutionLinkEvidence.append(textNode("li", value)));
  for (const link of links) {
    if (!link.source_asset_id || link.source_asset_id === state.solutionLink?.source_asset_id) continue;
    const row = document.createElement("li");
    const choose = textNode("button", `후보 이미지 선택 · ${link.status === "verified" ? "자동 확인됨" : "검토 필요"}`, "button");
    choose.type = "button";
    choose.disabled = state.busy;
    choose.addEventListener("click", async () => {
      if (state.dirty) { notify("편집 중인 내용을 먼저 저장해 주세요.", "error"); return; }
      choose.disabled = true;
      try {
        await api(`${endpoint("projects", state.projectId, "items", state.itemId)}/solution-source`, { method: "POST", body: {
          operation_id: `solution-${crypto.randomUUID()}`, expected_revision: state.revision,
          expected_selection_revision: state.package?.provenance?.solution_selection?.revision || 0,
          source: { kind: "candidate", source_asset_id: link.source_asset_id }
        } });
        await loadItem();
      } catch (error) { notify(error.message || "후보를 선택하지 못했습니다.", "error"); choose.disabled = false; }
    });
    row.append(choose);
    elements.solutionLinkEvidence.append(row);
  }
  elements.solutionLinkDetails.dataset.status = String(state.solutionLink?.status || "missing");
  elements.solutionLinkDetails.open = false;
  renderSolutionPropagationControls();
  renderSolutionFeedbackControls();
}

function trashEntryName(entry) {
  if (entry.name) return String(entry.name);
  if (entry.kind === "project") {
    const project = state.projects.find((value) => projectId(value) === entry.project_id);
    return String(project?.metadata?.filename_base ?? project?.title ?? project?.name ?? entry.project_id);
  }
  const item = state.items.find((value) => itemId(value) === entry.item_id);
  return String(item?.identity?.printed_number ?? item?.printed_number ?? entry.item_id);
}

function renderTrash(payload) {
  clearChildren(elements.trashList);
  const entries = Array.isArray(payload?.entries) ? payload.entries : [];
  const pending = entries.filter((entry) => entry.status === "deleting").length;
  elements.trashStatus.textContent = pending
    ? `완전 삭제 작업 ${pending}개가 중단되어 있습니다. 각 항목에서 이어서 실행하세요.`
    : entries.length ? `${entries.length}개 · 삭제 시각과 소속은 각 항목에 표시됩니다.` : "휴지통이 비어 있습니다.";
  for (const entry of entries) {
    const li = document.createElement("li");
    li.className = "trash-entry";
    const detail = document.createElement("span");
    detail.className = "trash-entry-copy";
    detail.append(textNode("strong", trashEntryName(entry)));
    detail.append(textNode("span", entry.kind === "project"
      ? `프로젝트 전체 · ${Number(entry.item_count) || 0}문항`
      : `${entry.project_id} · 개별 문항`));
    detail.append(textNode("time", entry.deleted_at ? new Date(entry.deleted_at).toLocaleString("ko-KR") : "삭제 시각 없음"));
    if (entry.status === "deleting") detail.append(textNode("span", "완전 삭제 진행 중 · 복원은 완료 후 가능합니다."));
    li.append(detail);
    const actions = document.createElement("div");
    actions.className = "trash-entry-actions";
    if (entry.status === "deleting") {
      const resume = textNode("button", "완전 삭제 이어서 실행", "trash-action trash-action-danger");
      resume.type = "button";
      resume.addEventListener("click", () => resumePurge(entry));
      actions.append(resume);
    } else {
      const restore = textNode("button", "복원", "trash-action");
      restore.type = "button";
      restore.addEventListener("click", () => restoreFromTrash(entry));
      const purge = textNode("button", "완전 삭제", "trash-action trash-action-danger");
      purge.type = "button";
      purge.addEventListener("click", () => purgeFromTrash(entry));
      actions.append(restore, purge);
    }
    li.append(actions);
    elements.trashList.append(li);
  }
}

async function loadTrash() {
  try {
    renderTrash(await api(endpoint("trash")));
  } catch (error) {
    elements.trashStatus.textContent = error.message || "휴지통을 불러오지 못했습니다.";
  }
}

function trashTargetLabel(preview) {
  return preview.kind === "project" ? (preview.name || preview.project_id) : `${preview.project_id} · ${preview.name || preview.item_id}`;
}

function trashQuestionSetSummary(questionSets = []) {
  const count = questionSets.reduce((sum, entry) => sum + Number(entry.remove_count || 0), 0);
  return questionSets.length
    ? `\n영향받는 문제지 ${questionSets.length}개 · 정리 문항 ${count}개\n${questionSets.map((entry) => `• ${entry.title || entry.set_id}: ${entry.remove_count}개`).join("\n")}`
    : "\n기존 문제지 참조는 없습니다.";
}

async function moveToTrash(kind, projectIdValue, itemIdValue = null) {
  if (state.busy) return;
  const target = { kind, project_id: projectIdValue, ...(itemIdValue ? { item_id: itemIdValue } : {}) };
  const hasDraft = state.projectId === projectIdValue && hasUnsavedWork()
    && (kind === "project" || state.itemId === itemIdValue);
  try {
    const preview = await api(endpoint("trash", "preview"), { method: "POST", body: target });
    if (preview.busy) throw new Error("진행 중인 작업이 있어 지금은 휴지통으로 옮길 수 없습니다.");
    if (hasDraft && !confirmDiscard()) return;
    await api(endpoint("trash", "move"), { method: "POST", body: { ...target, expected_trash_revision: preview.trash_revision, target_fingerprint: preview.target_fingerprint } });
    if (kind === "project") {
      clearProjectBankSelections(projectIdValue);
      if (state.projectId === projectIdValue) {
        state.projectId = null;
        resetItems("프로젝트를 휴지통으로 이동했습니다.");
      }
    } else {
      const key = `${projectIdValue}:${itemIdValue}`;
      state.bankSelection.delete(key);
      saveBankSelection();
      if (state.projectId === projectIdValue && state.itemId === itemIdValue) {
        state.itemId = null;
        state.itemSummary = null;
        closeWorkspace();
      }
    }
    elements.trashStatus.textContent = "";
    await Promise.all([loadProjects(state.projectId), loadTrash(), ...(state.view === "bank" ? [loadBankItems()] : [])]);
  } catch (error) {
    elements.trashStatus.textContent = error.message || "휴지통으로 이동하지 못했습니다.";
  }
}

async function restoreFromTrash(entry) {
  try {
    const current = await api(endpoint("trash"));
    await api(endpoint("trash", "restore"), {
      method: "POST",
      body: { kind: entry.kind, project_id: entry.project_id, ...(entry.item_id ? { item_id: entry.item_id } : {}), expected_trash_revision: current.trash_revision }
    });
    elements.trashStatus.textContent = "복원했습니다.";
    await Promise.all([loadProjects(state.projectId), loadTrash(), ...(state.view === "bank" ? [loadBankItems()] : [])]);
  } catch (error) {
    elements.trashStatus.textContent = error.message || "복원하지 못했습니다.";
  }
}

async function purgeFromTrash(entry) {
  const target = { kind: entry.kind, project_id: entry.project_id, ...(entry.item_id ? { item_id: entry.item_id } : {}) };
  try {
    const preview = await api(endpoint("trash", "purge-preview"), { method: "POST", body: target });
    const references = trashQuestionSetSummary(preview.question_sets);
    const shared = preview.shared?.length ? `\n다른 문항이 함께 쓰는 자료 ${preview.shared.length}개는 보존됩니다.` : "";
    const notice = preview.notice ? `\n${preview.notice}` : "";
    const label = trashTargetLabel(preview);
    const bytes = Number(preview.delete_bytes || 0).toLocaleString("ko-KR");
    const files = Array.isArray(preview.files) ? preview.files : [];
    const fileList = files.length ? `\n삭제 파일:\n${files.slice(0, 8).map((file) => `• ${file.path}`).join("\n")}${files.length > 8 ? `\n외 ${files.length - 8}개` : ""}` : "\n삭제 파일이 없습니다.";
    if (!window.confirm(`${label}을(를) 완전히 삭제합니다.\n삭제 파일 ${files.length}개 · ${bytes}바이트${fileList}${references}${shared}${notice}\n삭제 후 복원할 수 없습니다. 계속할까요?`)) return;
    elements.trashStatus.textContent = `${label} 완전 삭제를 진행 중입니다…`;
    const expectedRevisions = Object.fromEntries((preview.question_sets || []).map((set) => [set.set_id, set.revision]));
    const result = await api(endpoint("trash", "purge"), {
      method: "POST",
      body: { ...target, manifest_digest: preview.manifest_digest, expected_trash_revision: preview.trash_revision, expected_revisions: expectedRevisions, operation_id: `purge-${crypto.randomUUID()}`, confirmed: true }
    });
    if (result?.status !== "purged") throw new Error("완전 삭제 완료 상태를 확인하지 못했습니다.");
    const key = `${entry.project_id}:${entry.item_id || ""}`;
    state.bankSelection.delete(key);
    saveBankSelection();
    if (entry.kind === "project") {
      clearProjectBankSelections(entry.project_id);
      if (state.projectId === entry.project_id) {
        state.projectId = null;
        resetItems("프로젝트를 완전히 삭제했습니다.");
      }
    } else if (entry.kind === "item" && state.projectId === entry.project_id && state.itemId === entry.item_id) {
      state.itemId = null;
      state.itemSummary = null;
      closeWorkspace();
    }
    elements.trashStatus.textContent = "완전 삭제했습니다. 복원할 수 없습니다.";
    await Promise.all([loadProjects(state.projectId), loadTrash(), ...(state.view === "bank" ? [loadBankItems()] : [])]);
  } catch (error) {
    elements.trashStatus.textContent = error.message || "완전 삭제하지 못했습니다.";
  }
}

async function resumePurge(entry) {
  const target = { kind: entry.kind, project_id: entry.project_id, ...(entry.item_id ? { item_id: entry.item_id } : {}) };
  try {
    const result = await api(endpoint("trash", "purge"), {
      method: "POST",
      body: { ...target, manifest_digest: entry.manifest_digest, expected_trash_revision: entry.expected_trash_revision,
        expected_revisions: entry.expected_revisions, operation_id: entry.operation_id, confirmed: true }
    });
    if (result?.status !== "purged") throw new Error("완전 삭제 완료 상태를 확인하지 못했습니다.");
    const key = `${entry.project_id}:${entry.item_id || ""}`;
    state.bankSelection.delete(key);
    saveBankSelection();
    if (entry.kind === "project") {
      clearProjectBankSelections(entry.project_id);
      if (state.projectId === entry.project_id) {
        state.projectId = null;
        resetItems("프로젝트 완전 삭제를 마쳤습니다.");
      }
    } else if (entry.kind === "item" && state.projectId === entry.project_id && state.itemId === entry.item_id) {
      state.itemId = null;
      state.itemSummary = null;
      closeWorkspace();
    }
    elements.trashStatus.textContent = "중단됐던 완전 삭제를 마쳤습니다. 복원할 수 없습니다.";
    await Promise.all([loadProjects(state.projectId), loadTrash(), ...(state.view === "bank" ? [loadBankItems()] : [])]);
  } catch (error) {
    elements.trashStatus.textContent = error.message || "완전 삭제를 이어서 실행하지 못했습니다.";
    notify(error.message || "완전 삭제를 이어서 실행하지 못했습니다.", "error");
  }
}

elements.showSolutionCandidates.addEventListener("click", () => {
  elements.solutionLinkDetails.open = true;
  elements.solutionLinkDetails.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

function clearSolutionImages() {
  for (const file of state.solutionImages) URL.revokeObjectURL(file.preview);
  state.solutionImages = [];
  renderSolutionImages();
}

function addSolutionImages(files) {
  const images = [...files].filter((file) => ["image/png", "image/jpeg", "image/webp"].includes(file.type));
  if (images.length !== files.length || state.solutionImages.length + images.length > 8) {
    elements.solutionImageStatus.textContent = "PNG, JPEG, WEBP 이미지를 최대 8장까지 추가할 수 있습니다.";
    return;
  }
  for (const file of images) state.solutionImages.push({ file, preview: URL.createObjectURL(file) });
  renderSolutionImages();
}

function renderSolutionImages() {
  if (!elements.solutionImageQueue) return;
  elements.solutionImageQueue.replaceChildren();
  state.solutionImages.forEach((entry, index) => {
    const row = document.createElement("li");
    const picture = document.createElement("img");
    picture.src = entry.preview;
    picture.alt = `해설 이미지 ${index + 1} 미리보기`;
    const title = document.createElement("span");
    title.textContent = `${index + 1}. ${entry.file.name || "붙여넣은 이미지"}`;
    row.append(picture, title);
    for (const [label, target] of [["↑", index - 1], ["↓", index + 1]]) {
      if (target < 0 || target >= state.solutionImages.length) continue;
      const button = document.createElement("button");
      button.type = "button"; button.textContent = label; button.setAttribute("aria-label", `${index + 1}번째 이미지 ${label === "↑" ? "위로" : "아래로"}`);
      button.addEventListener("click", () => {
        [state.solutionImages[index], state.solutionImages[target]] = [state.solutionImages[target], state.solutionImages[index]];
        renderSolutionImages();
      });
      row.append(button);
    }
    const remove = document.createElement("button");
    remove.type = "button"; remove.textContent = "삭제";
    remove.addEventListener("click", () => {
      URL.revokeObjectURL(entry.preview);
      state.solutionImages.splice(index, 1);
      renderSolutionImages();
    });
    row.append(remove);
    elements.solutionImageQueue.append(row);
  });
  elements.applySolutionImages.disabled = !state.solutionImages.length || !state.itemId;
}

async function applySolutionImages() {
  if (!state.itemId || !state.solutionImages.length || state.dirty) {
    elements.solutionImageStatus.textContent = "편집 중인 내용을 먼저 저장해 주세요.";
    return;
  }
  elements.applySolutionImages.disabled = true;
  elements.solutionImageStatus.textContent = "이미지를 저장하고 있습니다.";
  try {
    const base = endpoint("projects", state.projectId, "items", state.itemId);
    const uploads = [];
    for (const entry of state.solutionImages) {
      uploads.push(await api(`${base}/solution-images`, { method: "POST", rawBody: entry.file,
        headers: { "Content-Type": entry.file.type, "X-File-Name": encodeURIComponent(entry.file.name || "clipboard.png") } }));
    }
    const result = await api(`${base}/solution-source`, { method: "POST", body: {
      operation_id: `solution-${crypto.randomUUID()}`, expected_revision: state.revision,
      expected_selection_revision: state.package?.provenance?.solution_selection?.revision || 0,
      source: { kind: "uploads", upload_ids: uploads.map((upload) => upload.upload_id) }
    } });
    elements.solutionImageStatus.textContent = `새 해설 원본을 선택했습니다. 타이핑 작업 ${result.job_id}을 진행합니다.`;
    await loadItem();
  } catch (error) {
    elements.solutionImageStatus.textContent = error.message || "이미지를 적용하지 못했습니다.";
    renderSolutionImages();
  }
}

elements.solutionImageFiles.addEventListener("change", (event) => {
  addSolutionImages(event.target.files);
  event.target.value = "";
});
elements.solutionImageDrop.addEventListener("dragover", (event) => event.preventDefault());
elements.solutionImageDrop.addEventListener("click", () => elements.solutionImageDrop.focus());
elements.solutionImageDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  addSolutionImages(event.dataTransfer.files);
});
elements.solutionImageDrop.addEventListener("paste", (event) => {
  const files = [...event.clipboardData.items].filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
  if (files.length) { event.preventDefault(); addSolutionImages(files); }
});
elements.applySolutionImages.addEventListener("click", applySolutionImages);
async function revertSolutionSource(restoreContent) {
  if (!state.itemId || state.dirty) {
    elements.solutionImageStatus.textContent = "편집 중인 내용을 먼저 저장해 주세요.";
    return;
  }
  if (restoreContent && !window.confirm("현재 텍스트와 정답을 이전 결과로 바꿀까요?")) return;
  const revision = state.package?.provenance?.solution_selection?.revision;
  if (!Number.isInteger(revision) || revision < 2) return;
  try {
    const result = await api(`${endpoint("projects", state.projectId, "items", state.itemId)}/solution-source/revert`, {
      method: "POST", body: { operation_id: `solution-${crypto.randomUUID()}`, expected_revision: state.revision,
        expected_selection_revision: revision, target_selection_revision: revision - 1,
        restore_content: restoreContent ? "including_manual" : "auto_only" }
    });
    elements.solutionImageStatus.textContent = `이전 해설 이미지로 되돌렸습니다. 타이핑 작업 ${result.job_id}을 진행합니다.`;
    await loadItem();
  } catch (error) { elements.solutionImageStatus.textContent = error.message || "되돌리지 못했습니다."; }
}
elements.revertSolutionImage.addEventListener("click", () => revertSolutionSource(false));
elements.restoreSolutionContent.addEventListener("click", () => revertSolutionSource(true));

function solutionFeedbackTarget() {
  const asset = sourceAsset("explanation");
  const links = Array.isArray(state.package?.provenance?.solution_links)
    ? state.package.provenance.solution_links : [];
  const link = links.find((value) => value?.source_asset_id === asset?.asset_id
    && ["detailed_solution", "mixed"].includes(value?.kind));
  const parts = Array.isArray(link?.parts) ? link.parts : [];
  const index = Number(elements.solutionFeedbackPart.value || 0);
  const part = parts[index];
  if (!asset || !link || !part || !asset.sha256 || !link.document_sha256) return null;
  return { asset, link, part, index, links };
}

function solutionFeedbackSyncKey() {
  return `studio-solution-feedback-sync:${state.projectId}:${state.itemId}`;
}

function savedSolutionFeedbackSync() {
  const events = state.package?.provenance?.solution_feedback_events;
  const currentEvent = Array.isArray(events) ? [...events].reverse().find((value) => value?.payload?.phase === "applied") : null;
  if (!currentEvent?.event_id) return null;
  try {
    const saved = JSON.parse(sessionStorage.getItem(solutionFeedbackSyncKey()) || "null");
    return saved?.event_id === currentEvent.event_id ? saved : null;
  } catch {
    return null;
  }
}

function feedbackPartLabel(part) {
  const printed = Number.isInteger(part?.printed_page) ? `책 ${part.printed_page}쪽` : "인쇄 쪽수 없음";
  const box = part?.bbox || {};
  return `PDF ${part?.pdf_page ?? "?"}쪽 · ${printed} · 영역 ${box.left ?? "?"}, ${box.top ?? "?"}–${box.right ?? "?"}, ${box.bottom ?? "?"}`;
}

function feedbackEligibility(target) {
  if (!target) return "상세 해설 후보가 있어야 검토 결과를 기록할 수 있습니다.";
  if (state.dirty) return "편집 중인 내용을 먼저 저장해 주세요.";
  if (state.section !== "explanation") return "해설 탭을 열어 표시된 후보 원본을 먼저 확인해 주세요.";
  if (state.assetIdBySection.explanation !== target.asset.asset_id || !apiAssetUrl(target.asset)) return "표시할 후보 원본을 확인할 수 없습니다.";
  if ((target.link.evidence || []).includes("manual_user_confirmation")) return "직접 확정한 해설은 보호되어 여기서 바꿀 수 없습니다.";
  if (!elements.solutionFeedbackAttested.checked) return "직접 비교했다는 확인을 선택해 주세요.";
  return "";
}

function renderSolutionFeedbackSync() {
  const sync = state.solutionFeedbackSync;
  if (!sync) {
    elements.solutionFeedbackSync.textContent = "";
    elements.solutionFeedbackSync.removeAttribute("data-status");
    return;
  }
  const pending = sync.status === "pending";
  elements.solutionFeedbackSync.dataset.status = pending ? "pending" : "synced";
  elements.solutionFeedbackSync.textContent = pending
    ? `문항 저장됨 · 학습 반영 대기${sync.retryable ? " · 다시 시도 예정" : ""}`
    : "문항 저장됨 · 학습 반영됨";
}

function renderSolutionFeedbackControls() {
  const target = solutionFeedbackTarget();
  const parts = target?.link?.parts || [];
  elements.solutionFeedbackPanel.hidden = !target;
  if (!target) return;
  const selectedPart = String(target.index);
  elements.solutionFeedbackPart.replaceChildren(...parts.map((part, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.selected = option.value === selectedPart;
    option.textContent = feedbackPartLabel(part);
    return option;
  }));
  const hasQuickAnswer = target.links.some((value) => value?.kind === "quick_answer");
  elements.solutionFeedbackSource.textContent = `${hasQuickAnswer ? "빠른 정답과 상세 해설 후보" : "상세 해설 후보만"} · ${feedbackPartLabel(target.part)}`;
  [
    [elements.solutionFeedbackLeft, target.part.bbox.left], [elements.solutionFeedbackTop, target.part.bbox.top],
    [elements.solutionFeedbackRight, target.part.bbox.right], [elements.solutionFeedbackBottom, target.part.bbox.bottom],
  ].forEach(([input, value]) => { input.value = String(value); });
  const corrections = state.package?.provenance?.solution_corrections || [];
  const heading = corrections.find((value) => value?.operation === "heading_role" && value?.source?.candidate_asset_id === target.asset.asset_id);
  const alias = corrections.find((value) => value?.operation === "source_number_alias" && value?.source?.candidate_asset_id === target.asset.asset_id);
  elements.solutionFeedbackHeading.value = heading?.reviewed_value || "heading";
  elements.solutionFeedbackAliasValue.value = alias?.reviewed_value || "";
  const reason = feedbackEligibility(target);
  const disabled = Boolean(reason) || state.busy;
  [elements.solutionFeedbackCorrect, elements.solutionFeedbackReject, elements.solutionFeedbackCrop,
    elements.solutionFeedbackSaveHeading, elements.solutionFeedbackAlias].forEach((button) => { button.disabled = disabled; });
  elements.solutionFeedbackPart.disabled = state.busy;
  elements.solutionFeedbackAttested.disabled = state.busy;
  [elements.solutionFeedbackLeft, elements.solutionFeedbackTop, elements.solutionFeedbackRight, elements.solutionFeedbackBottom,
    elements.solutionFeedbackHeading, elements.solutionFeedbackAliasValue].forEach((input) => { input.disabled = state.busy; });
  elements.solutionFeedbackHelp.textContent = reason || "이 기록은 현재 문항에만 적용되며, 다른 책의 정답을 자동 확정하지 않습니다.";
  renderSolutionFeedbackSync();
}

function feedbackValue(operation) {
  if (operation === "crop_bounds") {
    const [left, top, right, bottom] = [elements.solutionFeedbackLeft, elements.solutionFeedbackTop,
      elements.solutionFeedbackRight, elements.solutionFeedbackBottom].map((input) => Number(input.value));
    if (![left, top, right, bottom].every(Number.isFinite) || !(0 <= left && left < right && right <= 1 && 0 <= top && top < bottom && bottom <= 1)) {
      throw new Error("영역은 0과 1 사이에서 왼쪽·위보다 오른쪽·아래가 커야 합니다.");
    }
    return { unit: "ratio", origin: "top-left", left, top, right, bottom };
  }
  if (operation === "heading_role") return elements.solutionFeedbackHeading.value;
  if (operation === "source_number_alias") {
    const value = elements.solutionFeedbackAliasValue.value.trim();
    if (!value) throw new Error("해설 원본 번호를 입력해 주세요.");
    return value;
  }
  return null;
}

async function applySolutionFeedback(operation) {
  const target = solutionFeedbackTarget();
  const reason = feedbackEligibility(target);
  if (state.busy || reason || !target) {
    if (reason) notify(reason, "error");
    return;
  }
  let value;
  try { value = feedbackValue(operation); }
  catch (error) { notify(error.message, "error"); return; }
  const source = {
    source_pdf_sha256: target.link.document_sha256, candidate_asset_id: target.asset.asset_id,
    candidate_asset_sha256: target.asset.sha256, pdf_index: target.part.pdf_index,
    pdf_page: target.part.pdf_page, printed_page: target.part.printed_page ?? null,
    role: target.link.kind, bbox: target.part.bbox,
  };
  setBusy(true);
  try {
    const plan = await api(endpoint("projects", state.projectId, "items", state.itemId, "solution-feedback", "preflight"), {
      method: "POST", body: { expected_revision: state.revision, operation,
        label_scope: { correct_pair: "content_match", rejected_pair: "content_match", crop_bounds: "crop_bounds", heading_role: "unit_boundary", source_number_alias: "number_mapping" }[operation],
        source, value, attested: true }
    });
    if (!window.confirm("표시된 원본을 검토한 결과만 현재 문항에 저장할까요?")) return;
    const result = await api(endpoint("projects", state.projectId, "items", state.itemId, "solution-feedback", "apply"), {
      method: "POST", body: { expected_revision: plan.revision, plan_sha256: plan.plan_sha256, confirmed: true }
    });
    state.solutionFeedbackSync = { ...result.learning_sync, event_id: result.event?.event_id };
    try { sessionStorage.setItem(solutionFeedbackSyncKey(), JSON.stringify(state.solutionFeedbackSync)); } catch {}
    await loadItem();
    notify(state.solutionFeedbackSync.status === "pending" ? "문항 저장됨 · 학습 반영 대기" : "검토 결과를 저장했습니다.");
  } catch (error) {
    notify(error.status === 409 ? "다른 화면에서 내용이 변경되었습니다. 문항을 다시 불러와 확인해 주세요." : error.message || "검토 결과를 저장하지 못했습니다.", "error");
  } finally {
    setBusy(false);
    renderSolutionFeedbackControls();
  }
}

function selectedSolutionPropagationAsset() {
  return sourceAsset("explanation");
}

function solutionPropagationReason(value) {
  const text = String(value || "");
  if (/fixed-edge|snapshot/i.test(text)) return "현재 연결 결과에는 범위 재확인 정보가 없습니다.";
  if (/mixed/i.test(text)) return "답과 해설이 섞인 원본은 범위 재확인을 지원하지 않습니다.";
  if (/exactly one|identity/i.test(text)) return "선택한 해설의 원본 연결 ID를 하나로 확정할 수 없습니다.";
  if (/anchor|links changed/i.test(text)) return "상위 연결 상태가 바뀌어 문항을 다시 불러와야 합니다.";
  return text || "이 해설은 범위 재확인을 사용할 수 없습니다.";
}

function solutionPropagationEligibility() {
  const asset = selectedSolutionPropagationAsset();
  const capability = state.solutionPropagation;
  if (!asset) return { eligible: false, asset: null, reason: "먼저 해설 원본을 선택하세요." };
  if (sourceAssetKind(asset) === "mixed") {
    return { eligible: false, asset, reason: "답과 해설이 섞인 원본은 범위 재확인을 지원하지 않습니다." };
  }
  const eligibleIds = Array.isArray(capability?.eligible_source_asset_ids)
    ? capability.eligible_source_asset_ids : [];
  if (!eligibleIds.includes(asset.asset_id)) {
    const reason = solutionPropagationReason(capability?.source_asset_reasons?.[asset.asset_id]
      || capability?.disabled_reason
      || "이 해설에는 범위 재확인용 원본 연결 ID가 없습니다.");
    return { eligible: false, asset, reason };
  }
  return { eligible: true, asset, reason: "선택한 해설을 고정하고 같은 범위의 검증된 연결만 다시 평가합니다." };
}

function renderSolutionPropagationControls() {
  if (!elements.solutionPropagationToggle) return;
  const { eligible, reason } = solutionPropagationEligibility();
  if (!eligible) {
    elements.solutionPropagationToggle.checked = false;
    state.solutionPropagationPreview = null;
  }
  elements.solutionPropagationToggle.disabled = state.busy || !eligible;
  elements.solutionPropagationHelp.textContent = reason;
  elements.previewSolutionPropagation.disabled = state.busy || !eligible
    || !elements.solutionPropagationToggle.checked;
  const preview = state.solutionPropagationPreview;
  elements.solutionPropagationPreview.hidden = !preview;
  elements.applySolutionPropagation.disabled = state.busy || !preview;
  if (!preview) {
    elements.solutionPropagationScope.textContent = "";
    elements.solutionPropagationCounts.textContent = "";
    return;
  }
  const counts = preview.counts || {};
  elements.solutionPropagationScope.textContent = `${preview.scope_label || "연결 범위"} · ${Number(preview.scope_item_count) || 0}문항`;
  elements.solutionPropagationCounts.textContent = [
    `직접 확정 ${Number(counts.manual_verified) || 0}`,
    `자동 확정 ${Number(counts.auto_verified) || 0}`,
    `검토 유지 ${Number(counts.review) || 0}`
  ].join(" · ");
}

function invalidateSolutionPropagationPreview(message = "") {
  state.solutionPropagationPreview = null;
  renderSolutionPropagationControls();
  if (message) elements.solutionPropagationHelp.textContent = message;
}

async function previewSolutionPropagation() {
  if (state.busy || !state.projectId || !state.itemId) return;
  const { eligible, asset } = solutionPropagationEligibility();
  if (!eligible || !asset || !elements.solutionPropagationToggle.checked) return;
  setBusy(true);
  try {
    const preview = await api(
      endpoint("projects", state.projectId, "items", state.itemId, "solutions", "propagation", "preview"),
      {
        method: "POST",
        body: {
          expected_revision: state.revision,
          source_asset_id: asset.asset_id,
          propagate_scope: true
        }
      }
    );
    state.solutionPropagationPreview = preview;
    renderSolutionPropagationControls();
  } catch (error) {
    invalidateSolutionPropagationPreview(
      error.status === 409 ? "연결 상태가 바뀌었습니다. 문항을 다시 불러온 뒤 미리 보세요." : error.message
    );
    notify(error.message || "범위 재확인 영향을 계산하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function applySolutionPropagation() {
  const preview = state.solutionPropagationPreview;
  if (state.busy || !preview || !state.projectId || !state.itemId) return;
  if (!window.confirm("선택한 해설을 확정하고 같은 범위의 연결을 재평가할까요? 원격 AI는 호출하지 않습니다.")) return;
  setBusy(true);
  try {
    const result = await api(
      endpoint("projects", state.projectId, "items", state.itemId, "solutions", "propagation", "apply"),
      {
        method: "POST",
        body: {
          preview_id: preview.preview_id,
          preview_sha256: preview.preview_sha256,
          confirmed: true
        }
      }
    );
    state.solutionPropagationPreview = null;
    await loadItem();
    notify(`해설을 확정하고 같은 범위 ${Number(result.scope_item_count) || 0}문항을 재평가했습니다.`);
  } catch (error) {
    invalidateSolutionPropagationPreview(
      error.status === 409 ? "연결 상태가 바뀌어 적용하지 않았습니다. 다시 미리 보세요." : error.message
    );
    notify(error.message || "범위 재확인을 적용하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

function sourceAssetKind(asset) {
  const links = state.package?.provenance?.solution_links;
  if (!Array.isArray(links)) return null;
  return links.find((link) => link?.source_asset_id === asset?.asset_id)?.kind ?? null;
}

function sourceAssetLabel(asset) {
  const kind = sourceAssetKind(asset);
  if (kind === "detailed_solution") return "상세 해설";
  if (kind === "quick_answer") return "빠른 정답";
  if (asset?.section === "shared") return "공유 원본";
  return asset?.section === "question" ? "문제" : "해설 원본";
}

function sourceAssets(section) {
  const assets = Array.isArray(state.package?.resources?.assets) ? state.package.resources.assets : [];
  const priority = { detailed_solution: 0, quick_answer: 1 };
  const draftSourceId = state.figureRecropDraft?.section === section ? state.figureRecropDraft.sourceAssetId : null;
  return assets
    .filter((asset) => (asset?.section === section || (asset?.section === "shared" && asset?.asset_id === draftSourceId))
      && (asset?.role === "source_crop" || asset?.asset_id === draftSourceId))
    .sort((a, b) => (priority[sourceAssetKind(a)] ?? 2) - (priority[sourceAssetKind(b)] ?? 2));
}

function sourceAsset(section) {
  const assets = sourceAssets(section);
  const selectedId = state.assetIdBySection[section];
  const selected = assets.find((asset) => asset.asset_id === selectedId);
  const asset = selected ?? assets[0] ?? null;
  state.assetIdBySection[section] = asset?.asset_id ?? null;
  return asset;
}

function apiAssetUrl(asset) {
  if (!asset) return null;
  const raw = state.assetUrls?.[asset.asset_id] ?? asset.api_url ?? asset.url;
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const url = new URL(raw, window.location.href);
    const localHosts = new Set([window.location.hostname, "localhost", "127.0.0.1", "::1"]);
    if (!["http:", "https:"].includes(url.protocol) || !localHosts.has(url.hostname)) return null;
    return url.href;
  } catch {
    return null;
  }
}

function renderSource() {
  renderSectionLabels();
  const assets = sourceAssets(state.section);
  const asset = sourceAsset(state.section);
  const url = apiAssetUrl(asset);
  elements.tabs.forEach((tab) => {
    const selected = tab.dataset.section === state.section;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  const activeTab = elements.tabs.find((tab) => tab.dataset.section === state.section);
  elements.sourceView.setAttribute("aria-labelledby", activeTab.id);
  elements.sourceAssetSelect.replaceChildren(...assets.map((candidate, index) => {
    const option = document.createElement("option");
    option.value = candidate.asset_id;
    option.textContent = `${sourceAssetLabel(candidate)}${assets.length > 1 ? ` ${index + 1}` : ""}`;
    option.selected = candidate.asset_id === asset?.asset_id;
    return option;
  }));
  elements.sourceAssetSelect.hidden = assets.length < 2;
  elements.sourceCanvas.hidden = !url;
  elements.sourcePlaceholder.hidden = Boolean(url);
  elements.sourcePlaceholder.querySelector("strong").textContent = "표시할 이미지가 없습니다.";
  if (url) {
    elements.sourceImage.src = url;
    elements.sourceImage.alt = `${state.section === "question" ? "문제" : "해설"} 원본 이미지`;
  } else {
    elements.sourceImage.removeAttribute("src");
    elements.sourceImage.alt = "";
  }
  elements.sourceAssetLabel.textContent = asset ? sourceAssetLabel(asset) : "원본 이미지 없음";
  renderSourceOverlays();
}

function editorPackage() {
  return parseEditor(false) ?? state.package;
}

function walkBlocks(blocks, visit) {
  if (!Array.isArray(blocks)) return;
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    visit(block);
    for (const group of nestedBlockGroups(block)) walkBlocks(group.blocks, visit);
  }
}

function walkAnswerBlocks(answer, visit) {
  if (!answer || typeof answer !== "object") return;
  if (answer.kind === "content") walkBlocks(answer.blocks, visit);
  if (answer.kind === "composite") {
    for (const item of answer.items || []) walkAnswerBlocks(item?.answer, visit);
  }
}

function firstBlockId(packageData, section) {
  let first = null;
  walkBlocks(packageData?.content?.[section]?.blocks, (block) => {
    if (!first && block.block_id) first = block.block_id;
  });
  return first;
}

function findBlock(packageData, blockId) {
  let found = null;
  for (const section of ["question", "explanation"]) {
    walkBlocks(packageData?.content?.[section]?.blocks, (block) => {
      if (!found && block.block_id === blockId) found = { block, section };
    });
  }
  walkAnswerBlocks(packageData?.content?.answer, (block) => {
    if (!found && block.block_id === blockId) found = { block, section: "answer" };
  });
  return found;
}

function ratioBox(block, assetId) {
  const region = block?.source_region;
  const box = region?.bbox;
  if (region?.source_asset_id !== assetId || box?.unit !== "ratio" || box?.origin !== "top-left") return null;
  const values = [box.left, box.top, box.right, box.bottom].map(Number);
  if (!values.every(Number.isFinite)) return null;
  const [left, top, right, bottom] = values.map((value) => Math.max(0, Math.min(1, value)));
  return right > left && bottom > top ? { left, top, right, bottom } : null;
}

function adjustedBox(box, key, resize, step) {
  const next = { ...box };
  if (resize) {
    if (key === "ArrowLeft") next.right = Math.max(next.left + BBOX_MIN_SIZE, next.right - step);
    if (key === "ArrowRight") next.right = Math.min(1, next.right + step);
    if (key === "ArrowUp") next.bottom = Math.max(next.top + BBOX_MIN_SIZE, next.bottom - step);
    if (key === "ArrowDown") next.bottom = Math.min(1, next.bottom + step);
  } else {
    const dx = key === "ArrowLeft" ? -step : key === "ArrowRight" ? step : 0;
    const dy = key === "ArrowUp" ? -step : key === "ArrowDown" ? step : 0;
    const boundedX = Math.max(-next.left, Math.min(dx, 1 - next.right));
    const boundedY = Math.max(-next.top, Math.min(dy, 1 - next.bottom));
    next.left += boundedX;
    next.right += boundedX;
    next.top += boundedY;
    next.bottom += boundedY;
  }
  for (const name of ["left", "top", "right", "bottom"]) next[name] = Number(next[name].toFixed(6));
  return next;
}

function bboxLabel(blockId, box) {
  const values = [box.left, box.top, box.right, box.bottom].map((value) => value.toFixed(3)).join(", ");
  return `선택 영역: ${values}`;
}

function editSelectedBox(event, blockId, assetId) {
  if (state.busy) return;
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  const packageData = parseEditor(false);
  const match = packageData && findBlock(packageData, blockId);
  const box = match && ratioBox(match.block, assetId);
  if (!match || !box) return;
  if (match.block.type === "figure") {
    if (state.figureRecropDraft?.blockId !== blockId || state.figureRecropDraft.sourceAssetId !== assetId) {
      elements.bboxEditStatus.textContent = "먼저 그림 영역 수정을 시작해 주세요.";
      return;
    }
    const next = adjustedBox(state.figureRecropDraft.bbox, event.key, event.shiftKey, event.ctrlKey || event.metaKey ? BBOX_FINE_STEP : BBOX_STEP);
    state.figureRecropDraft.bbox = next;
    renderSourceOverlays(packageData);
    elements.bboxEditStatus.textContent = `${bboxLabel(blockId, next)} · 저장 전`;
    const replacement = [...elements.sourceOverlayLayer.children].find((node) => node.dataset.blockId === blockId);
    replacement?.focus({ preventScroll: true });
    return;
  }
  const next = adjustedBox(box, event.key, event.shiftKey, event.ctrlKey || event.metaKey ? BBOX_FINE_STEP : BBOX_STEP);
  Object.assign(match.block.source_region.bbox, next);
  elements.editor.value = JSON.stringify(packageData, null, 2);
  elements.editorError.textContent = "";
  state.selectedBlockId = blockId;
  setDirty(true);
  renderEditorView();
  renderPreview(packageData);
  renderSourceOverlays(packageData);
  elements.bboxEditStatus.textContent = bboxLabel(blockId, next);
  const replacement = [...elements.sourceOverlayLayer.children].find((node) => node.dataset.blockId === blockId);
  replacement?.focus({ preventScroll: true });
}

function renderSourceOverlays(packageData = editorPackage()) {
  clearChildren(elements.sourceOverlayLayer);
  elements.sourceOverlayLayer.hidden = !state.overlaysVisible;
  renderFigureReview(packageData);
  const asset = sourceAsset(state.section);
  const blocks = packageData?.content?.[state.section]?.blocks;
  if (!state.overlaysVisible || !asset || !Array.isArray(blocks)) return;
  walkBlocks(blocks, (block) => {
    const draft = state.figureRecropDraft?.blockId === block.block_id && state.figureRecropDraft.sourceAssetId === asset.asset_id
      ? state.figureRecropDraft : null;
    const box = draft?.bbox ?? ratioBox(block, asset.asset_id);
    if (!box || !block.block_id) return;
    const overlay = document.createElement("button");
    const label = blockDisplayLabel(block);
    overlay.type = "button";
    overlay.className = "source-overlay";
    overlay.dataset.blockId = block.block_id;
    overlay.dataset.blockType = block.type || "unknown";
    overlay.dataset.bboxEditable = String(block.type !== "figure" || Boolean(draft));
    overlay.dataset.recropDraft = String(Boolean(draft));
    overlay.dataset.active = String(block.block_id === state.selectedBlockId);
    overlay.setAttribute("aria-pressed", String(block.block_id === state.selectedBlockId));
    overlay.setAttribute("aria-label", block.type === "figure"
      ? draft ? `${label} 수정 영역. 방향키로 이동하고 Shift와 방향키로 크기를 조절합니다.` : `${label} 영역. 원본 그림과 비교할 수 있습니다.`
      : `${label} 영역. 방향키로 이동하고 Shift와 방향키로 크기를 조절합니다.`);
    if (block.type !== "figure" || draft) {
      overlay.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight ArrowUp ArrowDown Shift+ArrowLeft Shift+ArrowRight Shift+ArrowUp Shift+ArrowDown");
    }
    overlay.title = label;
    overlay.style.left = `${box.left * 100}%`;
    overlay.style.top = `${box.top * 100}%`;
    overlay.style.width = `${(box.right - box.left) * 100}%`;
    overlay.style.height = `${(box.bottom - box.top) * 100}%`;
    overlay.append(textNode("span", label));
    overlay.addEventListener("click", () => selectReviewBlock(block.block_id, state.section, true));
    overlay.addEventListener("keydown", (event) => editSelectedBox(event, block.block_id, asset.asset_id));
    elements.sourceOverlayLayer.append(overlay);
  });
}

function blockTypeLabel(type) {
  return ({ paragraph: "글", figure: "그림", choices: "선택지", bogi: "보기", table: "표", box: "설명 상자" })[type] || "내용";
}

function blockRoleLabel(role) {
  return ({ heading: "제목", stem: "문제", instruction: "질문", body: "본문", explanation: "해설", answer: "정답", note: "참고", caption: "그림 설명", condition: "조건", passage: "지문", main: "주요 그림" })[role] || "";
}

function blockDisplayLabel(block) {
  return [blockTypeLabel(block?.type), blockRoleLabel(block?.role)].filter(Boolean).join(" · ");
}

function selectReviewBlock(blockId, section, fromOverlay = false) {
  if (!blockId) return;
  if (state.figureRecropDraft && state.figureRecropDraft.blockId !== blockId) {
    if (!window.confirm("수정 중인 그림 영역을 버리고 다른 내용을 선택할까요?")) return;
    setFigureRecropDraft(null);
    state.assetIdBySection[state.section] = null;
    renderSource();
  }
  if (["question", "explanation"].includes(section) && section !== state.section) setSection(section);
  state.selectedBlockId = blockId;
  document.querySelectorAll("[data-block-id]").forEach((node) => {
    const active = node.dataset.blockId === blockId;
    node.dataset.active = String(active);
    if (node.matches(".source-overlay")) node.setAttribute("aria-pressed", String(active));
    if (node.tagName === "SUMMARY") node.setAttribute("aria-current", active ? "true" : "false");
  });
  const target = fromOverlay
    ? [...elements.preview.querySelectorAll("[data-block-id]")].find((node) => node.dataset.blockId === blockId)
    : [...elements.sourceOverlayLayer.children].find((node) => node.dataset.blockId === blockId);
  target?.scrollIntoView({ block: "nearest", inline: "nearest" });
  const packageData = editorPackage();
  const match = findBlock(packageData, blockId);
  const box = match && ratioBox(match.block, match.block.source_region?.source_asset_id);
  elements.bboxEditStatus.textContent = box
    ? `${bboxLabel(blockId, box)}${match.block.type === "figure" ? " · 그림 대조 전용" : ""}`
    : "이 내용에는 수정할 인식 영역이 없습니다.";
  renderFigureReview(packageData);
  renderBlockEditor(packageData);
}

function figureLineageContains(assetId, originalId, assetsById) {
  const seen = new Set();
  let current = assetId;
  while (current && !seen.has(current)) {
    if (current === originalId) return true;
    seen.add(current);
    current = assetsById.get(current)?.derived_from_asset_id;
  }
  return false;
}

function figureReviewLabel(status) {
  return ({ approved: "사용 가능", rejected: "제외", unreviewed: "확인 전" })[status] || "확인 전";
}

function renderFigureReview(packageData = editorPackage()) {
  clearChildren(elements.figureVariantList);
  const match = state.selectedBlockId && findBlock(packageData, state.selectedBlockId);
  const block = match?.block;
  if (!block || block.type !== "figure") {
    elements.figureReviewPanel.hidden = true;
    return;
  }
  const assets = Array.isArray(packageData?.resources?.assets) ? packageData.resources.assets : [];
  const assetsById = new Map(assets.filter((asset) => asset?.asset_id).map((asset) => [asset.asset_id, asset]));
  const variants = assets
    .filter((asset) => ["figure_original", "figure_regenerated"].includes(asset?.role))
    .filter((asset) => [match.section, "shared"].includes(asset.section))
    .filter((asset) => figureLineageContains(asset.asset_id, block.original_asset_id, assetsById))
    .sort((a, b) => Number(a.asset_id !== block.original_asset_id) - Number(b.asset_id !== block.original_asset_id));
  elements.figureReviewPanel.hidden = false;
  elements.figureReviewSummary.textContent = `${blockRoleLabel(block.role) || "그림"} · ${variants.length}개 · ${block.alt_text || "설명 없음"}`;
  const originalAsset = assetsById.get(block.original_asset_id);
  if (originalAsset?.role === "table_snapshot") {
    const card = document.createElement("article");
    card.className = "figure-variant-card";
    card.dataset.selected = "true";
    const imageUrl = apiAssetUrl(originalAsset);
    if (imageUrl) {
      const image = document.createElement("img");
      image.src = imageUrl;
      image.alt = block.alt_text || "표 이미지";
      card.append(image);
    }
    const copy = document.createElement("div");
    copy.className = "figure-variant-copy";
    copy.append(textNode("strong", "원본 표"));
    copy.append(textNode("p", "표 내용은 원본 이미지와 비교할 수 있습니다.", "figure-variant-meta"));
    card.append(copy);
    elements.figureVariantList.append(card);
    elements.figureReviewSummary.textContent = `원본 표 · ${block.alt_text || "설명 없음"}`;
    return;
  }
  const generation = document.createElement("div");
  generation.className = "figure-generation-actions";
  const generationCopy = document.createElement("div");
  generationCopy.append(textNode("strong", "그림 다시 만들기"));
  generationCopy.append(textNode("p", "원본 그림은 그대로 보관합니다."));
  const generate = textNode("button", "그림 다시 만들기", "button");
  generate.type = "button";
  const codexOnlyProject = state.projects.some(
    (project) => projectId(project) === state.projectId
      && project?.provider_profile === "codex_only"
  );
  const geminiConfigured = Object.values(state.figureProfiles).some((value) => value?.configured)
    || Object.values(state.providerProfiles).some((value) => value?.provider === "gemini" && value?.configured);
  generate.disabled = codexOnlyProject || !geminiConfigured;
  generate.title = codexOnlyProject
    ? "Codex-only 작업은 그림 재생성을 사용할 수 없습니다."
    : geminiConfigured ? "새 그림을 만듭니다." : "그림 처리 설정이 필요합니다.";
  generate.addEventListener("click", () => regenerateFigure(block.block_id, match.section, state.figureProfile, false));
  generation.append(generationCopy, generate);
  elements.figureVariantList.append(generation);
  const recrop = document.createElement("div");
  recrop.className = "figure-recrop-actions";
  const draft = state.figureRecropDraft?.blockId === block.block_id ? state.figureRecropDraft : null;
  if (draft) {
    recrop.append(textNode("code", bboxLabel(block.block_id, draft.bbox)));
    const create = textNode("button", "이 영역으로 다시 자르기", "button primary");
    create.type = "button";
    create.addEventListener("click", () => createFigureRecrop(block.block_id, match.section, draft.bbox));
    const cancel = textNode("button", "영역 수정 취소", "button");
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      setFigureRecropDraft(null);
      state.assetIdBySection[match.section] = null;
      renderSource();
      elements.bboxEditStatus.textContent = "그림 영역 수정을 취소했습니다.";
    });
    recrop.append(create, cancel);
  } else {
    const original = assetsById.get(block.original_asset_id);
    const originalBox = ratioBox({ source_region: original?.source_region }, original?.source_region?.source_asset_id);
    const originalSourceId = original?.source_region?.source_asset_id;
    const begin = textNode("button", "그림 영역 다시 자르기", "button");
    begin.type = "button";
    begin.disabled = !originalBox || !originalSourceId;
    begin.addEventListener("click", () => {
      if (state.busy) return;
      if (state.dirty) {
        notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
        return;
      }
      state.assetIdBySection[match.section] = originalSourceId;
      setFigureRecropDraft({ blockId: block.block_id, section: match.section, sourceAssetId: originalSourceId, bbox: { ...originalBox } });
      renderSource();
      elements.bboxEditStatus.textContent = "방향키로 그림 영역을 조정하세요.";
    });
    recrop.append(begin, textNode("span", "원본은 그대로 보관합니다."));
  }
  elements.figureVariantList.append(recrop);
  for (const asset of variants) {
    const review = asset.review && typeof asset.review === "object" ? asset.review : {};
    const status = String(review.status || "unreviewed");
    const selected = asset.asset_id === block.asset_id;
    const card = document.createElement("article");
    card.className = "figure-variant-card";
    card.dataset.status = status;
    card.dataset.selected = String(selected);
    const imageUrl = apiAssetUrl(asset);
    if (imageUrl) {
      const image = document.createElement("img");
      image.alt = block.alt_text || "문항 그림";
      image.loading = "eager";
      image.addEventListener("error", () => {
        image.replaceWith(
          textNode(
            "p",
            "그림 이미지를 불러올 수 없습니다.",
            "figure-image-fallback"
          )
        );
      });
      image.src = imageUrl;
      card.append(image);
    }
    const copy = document.createElement("div");
    copy.className = "figure-variant-copy";
    const heading = document.createElement("div");
    heading.className = "figure-variant-heading";
    heading.append(textNode("strong", asset.role === "figure_original" ? "원본 그림" : "새 그림"));
    const badge = textNode("span", `${figureReviewLabel(status)}${selected ? " · 사용 중" : ""}`, "figure-review-badge");
    badge.dataset.status = status;
    heading.append(badge);
    copy.append(heading);
    if (review.boundary_uncertain) copy.append(textNode("p", "잘린 부분이 있는지 확인해 주세요.", "figure-variant-meta"));
    const actions = document.createElement("div");
    actions.className = "figure-variant-actions";
    const approve = textNode("button", status === "approved" && !review.boundary_uncertain ? "사용 가능" : "사용 가능으로 표시", "button");
    approve.type = "button";
    approve.disabled = status === "approved" && !review.boundary_uncertain;
    approve.addEventListener("click", () => reviewFigureAsset(block.block_id, match.section, asset, "approved", false));
    const reject = textNode("button", status === "rejected" ? "제외됨" : "제외", "button");
    reject.type = "button";
    reject.disabled = status === "rejected";
    reject.addEventListener("click", () => reviewFigureAsset(block.block_id, match.section, asset, "rejected", false));
    const boundary = textNode("button", review.boundary_uncertain ? "잘림 없음" : "잘린 부분 있음", "text-button");
    boundary.type = "button";
    boundary.addEventListener("click", () => reviewFigureAsset(block.block_id, match.section, asset, "unreviewed", !review.boundary_uncertain));
    const choose = textNode("button", selected ? "사용 중" : "이 그림 사용", "button primary");
    choose.type = "button";
    choose.disabled = selected || asset.state !== "ready" || status !== "approved" || review.boundary_uncertain === true;
    choose.addEventListener("click", () => selectFigureVariant(block.block_id, match.section, asset.asset_id));
    actions.append(approve, reject, boundary, choose);
    copy.append(actions);
    card.append(copy);
    elements.figureVariantList.append(card);
  }
  if (!variants.length) elements.figureVariantList.append(textNode("p", "원본 그림을 찾을 수 없습니다.", "empty-note"));
}

async function regenerateFigure(blockId, section, profile, removeBlueText) {
  if (state.busy) return;
  if (requireFinishedRecrop("그림을 다시 만들기")) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "figures", blockId, "regenerate"), {
      method: "POST",
      body: { expected_revision: state.revision, profile, remove_blue_text: removeBlueText }
    });
    applyDetail(detailFrom(payload));
    selectReviewBlock(blockId, section);
    notify(payload?.cached ? "기존 새 그림을 다시 불러왔습니다." : "새 그림을 만들었습니다. 원본과 비교해 주세요.");
  } catch (error) {
    notify(error.message || "그림을 다시 만들지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function createFigureRecrop(blockId, section, bbox) {
  if (state.busy) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "figures", blockId, "recrop"), {
      method: "POST",
      body: { expected_revision: state.revision, bbox: { unit: "ratio", origin: "top-left", ...bbox } }
    });
    setFigureRecropDraft(null);
    applyDetail(detailFrom(payload));
    selectReviewBlock(blockId, section);
    notify(payload?.created === false ? "기존에 자른 영역을 다시 사용합니다." : "그림 영역을 다시 잘랐습니다. 원본과 비교해 주세요.");
  } catch (error) {
    notify(error.message || "그림 영역을 다시 자르지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function reviewFigureAsset(blockId, section, asset, status, boundaryUncertain) {
  if (state.busy) return;
  if (requireFinishedRecrop("그림을 검수")) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "assets", asset.asset_id, "review"), {
      method: "POST",
      body: {
        expected_revision: state.revision,
        status,
        boundary_uncertain: boundaryUncertain,
        notes: Array.isArray(asset.review?.notes) ? asset.review.notes : []
      }
    });
    applyDetail(detailFrom(payload));
    selectReviewBlock(blockId, section);
    notify(status === "approved" ? "이 그림을 사용할 수 있습니다." : status === "rejected" ? "이 그림을 제외했습니다." : "그림 잘림 상태를 저장했습니다.");
  } catch (error) {
    notify(error.message || "그림 검수 상태를 저장하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function selectFigureVariant(blockId, section, assetId) {
  if (state.busy) return;
  if (requireFinishedRecrop("그림을 선택")) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "figures", blockId, "select"), {
      method: "POST",
      body: { expected_revision: state.revision, asset_id: assetId }
    });
    applyDetail(detailFrom(payload));
    selectReviewBlock(blockId, section);
    notify("이 그림을 사용하도록 저장했습니다.");
  } catch (error) {
    notify(error.message || "그림을 선택하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

function setSection(section, focus = false) {
  if (section !== state.section) {
    if (state.figureRecropDraft && !window.confirm("수정 중인 그림 영역을 버리고 화면을 전환할까요?")) return false;
    state.selectedBlockId = null;
    setFigureRecropDraft(null);
    elements.bboxEditStatus.textContent = "";
  }
  state.section = section;
  state.selectedBlockId = firstBlockId(editorPackage(), section);
  renderSource();
  renderSolutionFeedbackControls();
  renderFigureReview();
  renderBlockEditor(editorPackage());
  renderPreview(editorPackage());
  renderCandidate();
  renderUnsavedState();
  if (focus) elements.tabs.find((tab) => tab.dataset.section === section).focus();
  return true;
}

function openEditorFor(section) {
  const selectedId = state.dualView[section].assetId;
  if (state.figureRecropDraft && section === state.section && selectedId !== state.figureRecropDraft.sourceAssetId
      && !window.confirm("수정 중인 그림 영역을 버리고 원본 이미지를 변경할까요?")) return;
  if (!setSection(section)) return;
  if (selectedId && selectedId !== state.assetIdBySection[section]) {
    if (state.figureRecropDraft) setFigureRecropDraft(null);
    state.assetIdBySection[section] = selectedId;
    renderSource();
  }
  const workbench = $("editorWorkbench");
  workbench.open = true;
  $("editorWorkbenchTitle").focus();
}

function parseEditor(showError = true) {
  try {
    const parsed = JSON.parse(elements.editor.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("최상위 값은 객체여야 합니다.");
    elements.editorError.textContent = "";
    return parsed;
  } catch (error) {
    if (showError) elements.editorError.textContent = `기술 정보 오류: ${error.message}`;
    return null;
  }
}

function renderEditorView() {
  const contentMode = state.editorView === "content";
  elements.contentViewTab.setAttribute("aria-pressed", String(contentMode));
  elements.jsonViewTab.setAttribute("aria-pressed", String(!contentMode));
  elements.contentView.hidden = !contentMode;
  elements.editor.hidden = contentMode;
  elements.format.hidden = contentMode;
  if (contentMode) {
    const documentValue = parseEditor(false) ?? state.package;
    renderSimpleEditor(documentValue);
  }
}

function simpleControl(tag, value = "") {
  const control = document.createElement(tag);
  control.value = String(value ?? "");
  control.disabled = state.busy || Boolean(state.figureRecropDraft);
  return control;
}

function labeledControl(labelText, control, hint = "") {
  const label = document.createElement("label");
  label.className = "simple-field";
  label.append(textNode("span", labelText));
  label.append(control);
  if (hint) label.append(textNode("small", hint));
  return label;
}

function commitSimpleEdit(packageData, refreshAnswer = false) {
  if (state.busy || state.figureRecropDraft) return;
  elements.editor.value = JSON.stringify(packageData, null, 2);
  elements.editorError.textContent = "";
  setDirty(true);
  state.lastValidPackage = packageData;
  renderPreview(packageData);
  renderDualReading();
  renderSourceOverlays(packageData);
  if (refreshAnswer) renderAnswerEditor(packageData);
}

function setInlineMark(inline, mark, enabled) {
  const marks = Array.isArray(inline.marks) ? inline.marks.filter((value) => value !== mark) : [];
  if (enabled) marks.push(mark);
  if (marks.length) inline.marks = marks;
  else delete inline.marks;
}

function renderParagraphFields(block, packageData) {
  const fragment = document.createDocumentFragment();
  for (const [index, inline] of (block.inlines || []).entries()) {
    if (inline?.type === "text") {
      const group = document.createElement("div");
      group.className = "simple-text-run";
      const input = simpleControl("textarea", inline.text);
      input.rows = 3;
      input.addEventListener("input", () => {
        inline.text = input.value;
        commitSimpleEdit(packageData);
      });
      group.append(labeledControl(`글 ${index + 1}`, input));

      const underline = simpleControl("input");
      underline.type = "checkbox";
      underline.checked = Array.isArray(inline.marks) && inline.marks.includes("underline");
      underline.setAttribute("aria-label", `글 ${index + 1} 밑줄 표시`);
      underline.addEventListener("change", () => {
        setInlineMark(inline, "underline", underline.checked);
        commitSimpleEdit(packageData);
      });
      const underlineLabel = document.createElement("label");
      underlineLabel.className = "simple-mark-toggle";
      underlineLabel.append(underline, textNode("span", "밑줄 표시"));
      group.append(underlineLabel);
      fragment.append(group);
      continue;
    }
    if (inline?.type === "equation") {
      const group = document.createElement("fieldset");
      group.className = "equation-fields";
      group.append(textNode("legend", `수식 ${index + 1}`));
      const source = simpleControl("textarea", inline.source_text);
      source.rows = 2;
      source.readOnly = true;
      group.append(labeledControl("원문 수식 (보존)", source));
      const latex = simpleControl("textarea", inline.latex);
      latex.rows = 2;
      latex.spellcheck = false;
      const script = simpleControl("textarea", inline.hwp_script);
      script.rows = 2;
      script.spellcheck = false;
      const manual = simpleControl("input");
      manual.type = "checkbox";
      manual.checked = inline.hwp_script_mode === "manual" || (inline.source_format === "hwp_script" && !inline.latex);
      script.readOnly = !manual.checked;
      const note = textNode("p", "", "structure-only-note");
      const refreshNote = () => {
        note.textContent = manual.checked
          ? "수동 수식으로 한글 출력 · LaTeX 미리보기"
          : (inline.hwp_derivation?.status === "unsupported"
            ? "변환 불가 · 원문 보존 · LaTeX 확인 필요"
            : "LaTeX 저장 시 한글 수식을 자동 변환합니다.");
      };
      latex.addEventListener("input", () => {
        inline.latex = latex.value;
        if (!manual.checked && latex.value) inline.source_format = "latex";
        if (!manual.checked) {
          delete inline.hwp_script;
          delete inline.hwp_derivation;
          script.value = "";
        }
        commitSimpleEdit(packageData);
        refreshNote();
      });
      manual.addEventListener("change", () => {
        inline.hwp_script_mode = manual.checked ? "manual" : "auto";
        script.readOnly = !manual.checked;
        if (!manual.checked && inline.latex) inline.source_format = "latex";
        delete inline.hwp_derivation;
        if (manual.checked && script.value) inline.hwp_script = script.value;
        if (!manual.checked) {
          delete inline.hwp_script;
          script.value = "";
        }
        commitSimpleEdit(packageData);
        refreshNote();
      });
      script.addEventListener("input", () => {
        inline.hwp_script_mode = "manual";
        inline.hwp_script = script.value;
        delete inline.hwp_derivation;
        commitSimpleEdit(packageData);
      });
      const manualLabel = document.createElement("label");
      manualLabel.className = "simple-mark-toggle";
      manualLabel.append(manual, textNode("span", "한글 수식 수동 지정"));
      group.append(labeledControl("LaTeX", latex), manualLabel, labeledControl("한글 수식", script), note);
      refreshNote();
      fragment.append(group);
      continue;
    }
    fragment.append(textNode("p", inline?.type === "line_break" ? `줄바꿈 ${index + 1}` : `지원하지 않는 inline ${index + 1}`, "structure-only-note"));
  }
  if (!block.inlines?.length) fragment.append(textNode("p", "편집할 inline이 없습니다.", "empty-note"));
  return fragment;
}

function renderItemLabelFields(block, packageData) {
  const fragment = document.createDocumentFragment();
  if (block.type === "bogi" && block.title && typeof block.title === "object") {
    const title = simpleControl("input", block.title.text);
    title.type = "text";
    title.addEventListener("input", () => {
      block.title.text = title.value;
      commitSimpleEdit(packageData);
    });
    fragment.append(labeledControl("제목", title));
  }
  for (const [index, item] of (block.items || []).entries()) {
    const label = simpleControl("input", item?.label);
    label.type = "text";
    label.addEventListener("input", () => {
      item.label = label.value;
      commitSimpleEdit(packageData, block.type === "choices");
    });
    const stableId = item?.choice_id ?? item?.item_id ?? "ID 없음";
    fragment.append(labeledControl(`항목 ${index + 1} 표기`, label, `고정 ID: ${stableId}`));
  }
  return fragment;
}

function renderBlockEditor(packageData) {
  clearChildren(elements.blockEditorFields);
  const match = state.selectedBlockId && findBlock(packageData, state.selectedBlockId);
  const block = match?.block;
  elements.blockEditorEmpty.hidden = Boolean(block);
  elements.selectedBlockLabel.textContent = block ? blockDisplayLabel(block) : "선택 안 됨";
  if (!block) return;
  if (block.type === "paragraph") {
    elements.blockEditorFields.append(renderParagraphFields(block, packageData));
  } else if (block.type === "figure") {
    const input = simpleControl("textarea", block.alt_text);
    input.rows = 4;
    input.addEventListener("input", () => {
      block.alt_text = input.value;
      commitSimpleEdit(packageData);
    });
    elements.blockEditorFields.append(labeledControl("그림 설명", input, "그림 자체는 원본 이미지에서 확인할 수 있습니다."));
  } else if (["bogi", "choices"].includes(block.type)) {
    elements.blockEditorFields.append(renderItemLabelFields(block, packageData));
  } else if (["table", "box"].includes(block.type)) {
    const detail = block.type === "table"
      ? `${block.column_count ?? "?"}열 · ${block.rows?.length ?? 0}행`
      : `${block.blocks?.length ?? 0}개 하위 내용`;
    elements.blockEditorFields.append(textNode("p", `${detail} · 안쪽 글자를 수정하려면 전체 타이핑 결과에서 해당 내용을 선택하세요.`, "structure-only-note"));
  } else {
    elements.blockEditorFields.append(textNode("p", "이 내용은 기술 정보 보기에서 확인할 수 있습니다.", "structure-only-note"));
  }
  setSimpleEditorDisabled(state.busy || Boolean(state.figureRecropDraft));
}

function questionChoices(packageData) {
  const choices = [];
  const seen = new Set();
  walkBlocks(packageData?.content?.question?.blocks, (block) => {
    if (block.type !== "choices") return;
    for (const item of block.items || []) {
      if (!item?.choice_id || seen.has(item.choice_id)) continue;
      seen.add(item.choice_id);
      choices.push({ id: item.choice_id, label: item.label || item.choice_id });
    }
  });
  return choices;
}

function questionStatementLabels(packageData) {
  const labels = new Map();
  walkBlocks(packageData?.content?.question?.blocks, (block) => {
    if (block.type !== "bogi") return;
    for (const item of block.items || []) {
      if (item?.item_id) labels.set(item.item_id, item.label || item.item_id);
    }
  });
  return labels;
}

function answerBlockChooser(blocks, packageData) {
  const wrapper = document.createElement("div");
  wrapper.className = "answer-block-list";
  let count = 0;
  walkBlocks(blocks, (block) => {
    if (!block.block_id) return;
    count += 1;
    const button = textNode("button", blockDisplayLabel(block), "answer-block-button");
    button.type = "button";
    button.dataset.blockId = block.block_id;
    button.disabled = state.busy || Boolean(state.figureRecropDraft);
    button.addEventListener("click", () => {
      selectReviewBlock(block.block_id, "answer");
      elements.blockEditorFields.querySelector("textarea, input, select, button")?.focus();
    });
    wrapper.append(button);
  });
  if (!count) wrapper.append(textNode("p", "편집할 정답 내용이 없습니다.", "empty-note"));
  return wrapper;
}

function renderChoiceAnswer(answer, container, packageData, path) {
  const choices = questionChoices(packageData);
  if (!choices.length) {
    container.append(textNode("p", "문제 선택지를 먼저 확인해 주세요.", "structure-only-note"));
    return;
  }
  const selected = Array.isArray(answer.choice_ids) ? answer.choice_ids : [];
  const missing = selected.filter((id) => !choices.some((choice) => choice.id === id));
  if (missing.length) container.append(textNode("p", "문제 선택지와 연결되지 않은 정답이 있습니다.", "answer-warning"));
  const fieldset = document.createElement("fieldset");
  fieldset.className = "answer-options";
  fieldset.append(textNode("legend", "정답 선택지 · 하나 이상 선택, 복수 선택 가능"));
  let lastSelected = selected.filter((id) => choices.some((choice) => choice.id === id));
  const checkboxes = [];
  for (const choice of choices) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = `answer-choice-${path.replace(/[^a-z0-9_-]/gi, "-")}`;
    checkbox.value = choice.id;
    checkbox.checked = selected.includes(choice.id);
    checkbox.disabled = state.busy || Boolean(state.figureRecropDraft);
    checkboxes.push(checkbox);
    checkbox.addEventListener("change", () => {
      const values = checkboxes.filter((input) => input.checked).map((input) => input.value);
      if (!values.length) {
        for (const input of checkboxes) input.checked = lastSelected.includes(input.value);
        notify("정답 선택은 한 개 이상 유지해야 합니다.", "error");
        return;
      }
      lastSelected = values;
      answer.choice_ids = values;
      commitSimpleEdit(packageData);
    });
    label.append(checkbox, document.createTextNode(choice.label));
    fieldset.append(label);
  }
  container.append(fieldset);
}

function renderTrueFalseAnswer(answer, container, packageData, path) {
  const labels = questionStatementLabels(packageData);
  for (const [index, item] of (answer.items || []).entries()) {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "answer-options inline-options";
    fieldset.append(textNode("legend", labels.get(item.statement_id) || "진술"));
    for (const [labelText, value] of [["참", true], ["거짓", false]]) {
      const label = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = `answer-tf-${path.replace(/[^a-z0-9_-]/gi, "-")}-${index}`;
      radio.checked = item.value === value;
      radio.disabled = state.busy || Boolean(state.figureRecropDraft);
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        item.value = value;
        commitSimpleEdit(packageData);
      });
      label.append(radio, document.createTextNode(labelText));
      fieldset.append(label);
    }
    container.append(fieldset);
  }
}

function renderAnswerNode(answer, container, packageData, path = "answer") {
  if (!answer || typeof answer !== "object") return;
  if (answer.kind === "choice") {
    renderChoiceAnswer(answer, container, packageData, path);
  } else if (answer.kind === "true_false") {
    renderTrueFalseAnswer(answer, container, packageData, path);
  } else if (answer.kind === "content") {
    container.append(textNode("p", "아래 정답 내용을 선택하면 위에서 수정할 수 있습니다.", "structure-only-note"));
    container.append(answerBlockChooser(answer.blocks, packageData));
  } else if (answer.kind === "composite") {
    for (const [index, item] of (answer.items || []).entries()) {
      const group = document.createElement("section");
      group.className = "composite-answer-item";
      group.append(textNode("h5", item.label || `항목 ${index + 1}`));
      group.append(textNode("p", `정답 형태: ${answerTypeLabel(item.answer?.kind)}`, "structure-only-note"));
      renderAnswerNode(item.answer, group, packageData, `${path}-${index}`);
      container.append(group);
    }
  } else {
    container.append(textNode("p", "정답 형식이 확정되지 않았습니다. 구조를 임의로 바꾸지 않습니다.", "structure-only-note"));
  }
}

function renderAnswerEditor(packageData) {
  clearChildren(elements.answerEditorFields);
  const answer = packageData?.content?.answer;
  elements.answerKindLabel.textContent = answerTypeLabel(answer?.kind);
  if (!answer) {
    elements.answerEditorFields.append(textNode("p", "저장된 정답이 없습니다.", "empty-note"));
    return;
  }
  renderAnswerNode(answer, elements.answerEditorFields, packageData);
  setSimpleEditorDisabled(state.busy || Boolean(state.figureRecropDraft));
}

function answerTypeLabel(kind) {
  return ({ choice: "선택형", content: "서술형", true_false: "참·거짓", composite: "복합형", unknown: "확인 필요" })[kind] || "미입력";
}

function renderSimpleEditor(packageData) {
  renderBlockEditor(packageData);
  renderAnswerEditor(packageData);
}

function setEditorView(view) {
  if (view === "content" && !parseEditor()) return;
  state.editorView = view;
  renderEditorView();
  (view === "content" ? elements.contentViewTab : elements.jsonViewTab).focus();
}

function schedulePreview() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(() => {
    const parsed = parseEditor(false);
    if (parsed) {
      state.lastValidPackage = parsed;
      renderPreview(parsed);
      renderDualReading();
      renderSourceOverlays(parsed);
    } else {
      for (const panel of Object.values(elements.readingPanels)) {
        if (panel) panel.querySelector(".dual-reading-status").textContent = "기술 정보 오류로 읽기 갱신을 멈췄습니다.";
      }
    }
  }, 250);
}

function renderPreview(packageData) {
  clearChildren(elements.preview);
  const content = packageData?.content;
  if (!content || typeof content !== "object") {
    elements.preview.append(textNode("p", "아직 전사된 content가 없습니다.", "empty-state"));
    return;
  }
  const section = state.section;
  const blocks = content?.[section]?.blocks;
  const wrapper = document.createElement("section");
  wrapper.className = "section-block";
  wrapper.append(textNode("h4", "읽기 보기"));
  if (Array.isArray(blocks) && blocks.length) {
    const reading = document.createElement("div");
    reading.className = "reading-preview";
    blocks.forEach((block) => reading.append(renderReadableBlock(block)));
    wrapper.append(reading);
    const structure = document.createElement("details");
    structure.className = "structure-editor";
    structure.append(textNode("summary", "순서·구조 편집"), renderBlockList(blocks, 0, section));
    wrapper.append(structure);
  } else {
    wrapper.append(textNode("p", `${section === "question" ? "문제" : "해설"} 타이핑 내용이 없습니다.`, "empty-note"));
  }
  elements.preview.append(wrapper);
}

function renderDualReading() {
  if (!state.package) return;
  const packageData = state.lastValidPackage ?? state.package;
  for (const section of ["question", "explanation"]) {
    const panel = elements.readingPanels[section];
    if (!panel) continue;
    const view = state.dualView[section];
    const assets = sourceAssets(section);
    const blocks = packageData.content?.[section]?.blocks;
    const mode = readingMode(view, assets.length > 0, Array.isArray(blocks) && blocks.length > 0);
    const selected = readingAsset(view, assets);
    view.assetId = selected?.asset_id ?? null;
    panel.querySelectorAll("[data-view-mode]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.viewMode === mode));
    });
    const picker = panel.querySelector("[data-reading-asset]");
    picker.replaceChildren(...assets.map((asset, index) => {
      const option = document.createElement("option");
      option.value = asset.asset_id;
      option.textContent = `${sourceAssetLabel(asset)}${assets.length > 1 ? ` ${index + 1}` : ""}`;
      return option;
    }));
    picker.value = selected?.asset_id ?? "";
    picker.hidden = mode !== "source" || assets.length < 2;
    picker.closest("label").hidden = picker.hidden;
    const body = panel.querySelector(".dual-reading-body");
    body.replaceChildren();
    if (mode === "source") {
      const url = apiAssetUrl(selected);
      if (url) {
        const image = document.createElement("img");
        image.src = url;
        image.alt = `${section === "question" ? "문제" : "해설"} 원본 이미지`;
        const full = document.createElement("a");
        full.href = url;
        full.target = "_blank";
        full.rel = "noopener";
        full.textContent = "원본 전체 보기";
        body.append(image, full);
      } else body.append(textNode("p", "표시할 원본 이미지가 없습니다. 읽기 보기로 전환할 수 있습니다.", "empty-note"));
    } else if (Array.isArray(blocks) && blocks.length) {
      const reading = document.createElement("div");
      reading.className = "reading-preview";
      blocks.forEach((block) => reading.append(renderReadableBlock(block, packageData)));
      body.append(reading);
    } else body.append(textNode("p", `${section === "question" ? "문제" : "해설"} 타이핑 내용이 없습니다.`, "empty-note"));
    body.scrollTop = view.scrollTop;
    panel.querySelector(".dual-reading-status").textContent = state.dirty && !parseEditor(false)
      ? "기술 정보 오류로 읽기 갱신을 멈췄습니다." : "";
    if (section === "explanation") {
      const savedAnswer = answerSummaryText(state.package);
      const draftAnswer = answerSummaryText(packageData);
      panel.querySelector("[data-answer-summary]").textContent = draftAnswer === savedAnswer
        ? `저장된 정답 ${savedAnswer}` : `편집 중 정답 ${draftAnswer} · 저장된 정답 ${savedAnswer}`;
    }
  }
}

function renderReadableInlines(inlines) {
  const fragment = document.createDocumentFragment();
  for (const inline of inlines || []) {
    if (inline?.type === "line_break") fragment.append(document.createElement("br"));
    else if (inline?.type === "equation") fragment.append(renderReadableEquation(inline));
    else if (inline?.text) {
      const text = document.createTextNode(inline.text);
      if (Array.isArray(inline.marks) && inline.marks.includes("underline")) {
        const underline = document.createElement("u");
        underline.className = "reading-underline";
        underline.append(text);
        fragment.append(underline);
      } else {
        fragment.append(text);
      }
    }
  }
  return fragment;
}

function readableEquationFallback(inline) {
  const source = inline?.latex || inline?.source_text || "";
  if (!source.includes("\\")) return source || "수식 표시 확인 필요";
  return source
    .replace(/\\(?:text|mathrm)\{([^{}]*)\}/g, "$1")
    .replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, "($1)/($2)")
    .replace(/\\(?:left|right)/g, "")
    .replace(/\\(?:times)/g, "×")
    .replace(/\\(?:lambda)/g, "λ")
    .replace(/\\(?:Delta)/g, "Δ")
    .replace(/\\(?:Phi)/g, "Φ")
    .replace(/\\(?:propto)/g, "∝")
    .replace(/\\(?:rightarrow)/g, "→")
    .replace(/\\(?:therefore)/g, "∴")
    .replace(/\\(?:ge)/g, "≥")
    .replace(/\\(?:dots)/g, "…")
    .replace(/\\(?:quad|,|;| )/g, " ")
    .replace(/[{}]/g, "")
    .replace(/\\[A-Za-z]+/g, "")
    .replace(/\s+/g, " ")
    .trim() || "수식 표시 확인 필요";
}

function renderReadableEquation(inline) {
  const wrapper = document.createElement("span");
  wrapper.className = "reading-equation";
  const latex = inline?.latex || inline?.source_text || "";
  wrapper.title = inline?.hwp_script_mode === "manual" ? "수동 한글 수식 사용: 미리보기는 LaTeX 기준" : (inline?.source_text || latex || "수식");
  try {
    if (!latex || !globalThis.katex?.render) throw new Error("KaTeX unavailable");
    globalThis.katex.render(latex, wrapper, {
      output: "mathml",
      throwOnError: true,
      trust: false,
      strict: "ignore",
    });
  } catch {
    wrapper.classList.add("reading-equation-fallback");
    wrapper.textContent = readableEquationFallback(inline);
  }
  return wrapper;
}

function renderReadableBlock(block, packageData = state.package) {
  if (block?.type === "paragraph") {
    const tag = block.role === "heading" ? "h5" : "p";
    const paragraph = document.createElement(tag);
    paragraph.className = block.role === "heading" ? "reading-heading" : `reading-paragraph reading-${block.role || "body"}`;
    paragraph.append(renderReadableInlines(block.inlines));
    if (block.role === "heading") {
      if (paragraph.firstChild?.nodeType === 3) paragraph.firstChild.textContent = paragraph.firstChild.textContent.replace(/^\|\s*/, "");
      if (paragraph.lastChild?.nodeType === 3) paragraph.lastChild.textContent = paragraph.lastChild.textContent.replace(/\s*\|$/, "");
    }
    if (!paragraph.textContent.trim()) paragraph.textContent = "내용 없음";
    return paragraph;
  }
  if (block?.type === "figure") {
    const figure = document.createElement("figure");
    figure.className = "reading-figure";
    const asset = (packageData?.resources?.assets || []).find((value) => value?.asset_id === block.asset_id);
    const url = apiAssetUrl(asset);
    if (url) {
      const image = document.createElement("img");
      image.src = url;
      image.alt = block.alt_text || "문항 그림";
      image.loading = "lazy";
      figure.append(image);
    }
    if (!url) figure.append(textNode("span", block.alt_text || "그림 설명 없음"));
    return figure;
  }
  if (["choices", "bogi"].includes(block?.type)) {
    const section = document.createElement("section");
    section.className = "reading-container";
    const title = typeof block.title === "string" ? block.title : block.title?.text;
    if (block.type === "bogi" && block.title?.visible !== false && title) section.append(textNode("h5", title));
    const list = document.createElement(block.type === "choices" ? "ol" : "ul");
    list.className = "reading-item-list";
    if (block.type === "choices") list.classList.add(`reading-layout-${block.source_layout || "vertical"}`);
    for (const item of block.items || []) {
      const li = document.createElement("li");
      const content = document.createElement("div");
      content.className = "reading-item-content";
      for (const child of item.blocks || []) content.append(renderReadableBlock(child, packageData));
      if (!content.children.length) content.append(textNode("span", "내용 없음"));
      li.append(textNode("strong", item.label || "•", "reading-label"), content);
      list.append(li);
    }
    section.append(list);
    return section;
  }
  if (block?.type === "table") {
    const section = document.createElement("section");
    section.className = "reading-container";
    const wrap = document.createElement("div");
    wrap.className = "reading-table-wrap";
    const table = document.createElement("table");
    const body = document.createElement("tbody");
    for (const row of block.rows || []) {
      const tr = document.createElement("tr");
      for (const cell of row.cells || []) {
        const td = document.createElement(cell.role === "header" ? "th" : "td");
        td.rowSpan = Number(cell.row_span || 1);
        td.colSpan = Number(cell.col_span || 1);
        for (const child of cell.blocks || []) td.append(renderReadableBlock(child, packageData));
        if (!td.children.length) td.textContent = " ";
        tr.append(td);
      }
      body.append(tr);
    }
    table.append(body);
    wrap.append(table);
    section.append(wrap);
    return section;
  }
  if (block?.type === "box") {
    const section = document.createElement("section");
    section.className = `reading-container reading-box reading-box-${block.role || "note"}`;
    const title = typeof block.title === "string" ? block.title : block.title?.text;
    if (block.title?.visible !== false && title) section.append(textNode("h5", title));
    for (const child of block.blocks || []) section.append(renderReadableBlock(child, packageData));
    return section;
  }
  return textNode("p", summarizeBlock(block) || "내용 있음", "reading-paragraph");
}

function renderBlockList(blocks, depth, section) {
  const list = document.createElement("ol");
  list.className = "block-tree";
  for (const [index, block] of blocks.entries()) {
    if (!block || typeof block !== "object") continue;
    const li = document.createElement("li");
    const details = document.createElement("details");
    details.open = depth === 0;
    if (block.block_id) {
      details.dataset.blockId = block.block_id;
      details.dataset.active = String(block.block_id === state.selectedBlockId);
    }
    const label = blockDisplayLabel(block);
    const heading = textNode("summary", label);
    if (block.block_id) {
      heading.dataset.blockId = block.block_id;
      heading.setAttribute("aria-current", block.block_id === state.selectedBlockId ? "true" : "false");
      heading.addEventListener("click", () => selectReviewBlock(block.block_id, section));
    }
    details.append(heading);
    const actions = document.createElement("div");
    actions.className = "block-actions";
    for (const [label, offset] of [["위로", -1], ["아래로", 1]]) {
      const button = textNode("button", label, "block-move-button");
      button.type = "button";
      button.disabled = !block.block_id || index + offset < 0 || index + offset >= blocks.length;
      button.setAttribute("aria-label", `${blockDisplayLabel(block)} 순서를 ${label} 이동`);
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        moveBlock(section, block.block_id, offset);
      });
      actions.append(button);
    }
    details.append(actions);
    const summary = summarizeBlock(block);
    if (summary) details.append(textNode("p", summary, "block-summary"));
    for (const group of nestedBlockGroups(block)) {
      const groupNode = document.createElement("div");
      groupNode.className = "block-group";
      groupNode.append(textNode("p", group.label));
      groupNode.append(renderBlockList(group.blocks, depth + 1, section));
      details.append(groupNode);
    }
    li.append(details);
    list.append(li);
  }
  return list;
}

function findBlockSiblings(blocks, blockId) {
  if (!Array.isArray(blocks)) return null;
  const index = blocks.findIndex((block) => block?.block_id === blockId);
  if (index >= 0) return { blocks, index };
  for (const block of blocks) {
    for (const group of nestedBlockGroups(block || {})) {
      const found = findBlockSiblings(group.blocks, blockId);
      if (found) return found;
    }
  }
  return null;
}

function moveBlock(section, blockId, offset) {
  if (state.busy || !blockId) return;
  if (requireFinishedRecrop("내용 순서를 변경")) return;
  const packageData = parseEditor();
  const siblings = packageData && findBlockSiblings(packageData?.content?.[section]?.blocks, blockId);
  const nextIndex = siblings ? siblings.index + offset : -1;
  if (!siblings || nextIndex < 0 || nextIndex >= siblings.blocks.length) return;
  [siblings.blocks[siblings.index], siblings.blocks[nextIndex]] = [siblings.blocks[nextIndex], siblings.blocks[siblings.index]];
  state.selectedBlockId = blockId;
  elements.editor.value = JSON.stringify(packageData, null, 2);
  elements.editorError.textContent = "";
  setDirty(true);
  renderEditorView();
  renderPreview(packageData);
  renderSourceOverlays(packageData);
  const moved = [...elements.preview.querySelectorAll("details[data-block-id]")]
    .find((node) => node.dataset.blockId === blockId);
  for (let node = moved; node; node = node.parentElement?.closest("details")) node.open = true;
  moved?.querySelector("summary")?.focus({ preventScroll: true });
  moved?.scrollIntoView({ block: "nearest" });
}

function summarizeBlock(block) {
  if (block.type === "paragraph") {
    return inlinePlainText(block.inlines);
  }
  if (block.type === "figure") return block.alt_text || "그림";
  if (block.type === "choices") return `${block.items?.length ?? 0}개 선택지`;
  if (block.type === "bogi") return `${block.title?.text ?? "보기"} · ${block.items?.length ?? 0}개 항목`;
  if (block.type === "table") return `${block.column_count ?? "?"}열 · ${block.rows?.length ?? 0}행`;
  return "";
}

function nestedBlockGroups(block) {
  const groups = [];
  if (Array.isArray(block.blocks)) groups.push({ label: "내용", blocks: block.blocks });
  if (Array.isArray(block.items)) {
    block.items.forEach((item, index) => {
      if (Array.isArray(item?.blocks)) groups.push({ label: item.label ?? item.item_id ?? item.choice_id ?? `항목 ${index + 1}`, blocks: item.blocks });
    });
  }
  if (Array.isArray(block.rows)) {
    block.rows.forEach((row, rowIndex) => (row?.cells || []).forEach((cell, cellIndex) => {
      if (Array.isArray(cell?.blocks)) groups.push({ label: `${rowIndex + 1}행 ${cellIndex + 1}열`, blocks: cell.blocks });
    }));
  }
  return groups;
}

function candidateDocument(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  return candidate.candidate ?? candidate.package ?? candidate.proposed ?? candidate.document ?? candidate.output ??
    (candidate.item_id ? candidate : null);
}

function currentCandidate() {
  const eligible = state.candidates.filter((entry) => {
    const metadata = entry?.metadata ?? entry;
    return metadata?.section === state.section && Number(metadata?.base_revision) === state.revision;
  });
  eligible.sort((a, b) => String(b?.metadata?.completed_at ?? "").localeCompare(String(a?.metadata?.completed_at ?? "")));
  if (eligible.length) return eligible[0];
  const metadata = state.candidate?.metadata ?? state.candidate;
  if (metadata?.section === state.section && Number(metadata?.base_revision) === state.revision) return state.candidate;
  return null;
}

function renderCandidate() {
  const candidate = currentCandidate();
  const proposed = candidateDocument(candidate);
  elements.candidatePanel.hidden = !proposed;
  elements.candidateEmpty.hidden = Boolean(proposed);
  elements.candidateCompare.hidden = !proposed;
  elements.applyCandidate.hidden = !proposed;
  if (!proposed) {
    elements.candidateCurrent.textContent = "";
    elements.candidateProposed.textContent = "";
    return;
  }
  elements.candidateCurrent.textContent = JSON.stringify(state.package, null, 2);
  elements.candidateProposed.textContent = JSON.stringify(proposed, null, 2);
}

async function transcribeItem() {
  if (state.busy || !state.package) return;
  if (requireFinishedRecrop("OCR을 실행")) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  const asset = sourceAsset(state.section);
  if (!asset) {
    notify(`${state.section === "question" ? "문제" : "해설"} 원본 이미지가 없습니다.`, "error");
    return;
  }
  setBusy(true);
  try {
    await api(endpoint("projects", state.projectId, "items", state.itemId, "transcribe"), {
      method: "POST",
      body: { source_asset_id: asset.asset_id, section: state.section, profile: elements.transcribeProfile.value }
    });
    await loadItem();
    notify("새 OCR 결과를 만들었습니다.");
  } catch (error) {
    notify(error.message || "글자 인식을 시작할 수 없습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function applyCandidate() {
  const candidate = currentCandidate();
  const jobId = candidate?.job_id ?? candidate?.metadata?.job_id;
  if (!jobId || state.busy) return;
  if (requireFinishedRecrop("새 OCR 결과를 적용")) return;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "candidates", jobId, "apply"), {
      method: "POST",
      body: { expected_revision: state.revision }
    });
    applyDetail(detailFrom(payload));
    notify("새 OCR 결과로 바꿨습니다.");
  } catch (error) {
    notify(error.message || "새 OCR 결과를 적용하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function approveItem() {
  if (state.busy || !state.package) return false;
  if (APPROVED_STATUSES.has(workflowOf(state.package))) {
    notify("이미 확인 완료로 표시된 문항입니다.");
    return false;
  }
  if (requireFinishedRecrop("확인 완료로 표시")) return false;
  if (state.dirty) {
    notify("편집 중인 내용을 먼저 저장해 주세요.", "error");
    return false;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "approve"), {
      method: "POST",
      body: { expected_revision: state.revision, reviewer: "local-user" }
    });
    applyDetail(detailFrom(payload));
    syncCurrentItemSummary();
    notify("문제·해설이 맞는 것으로 확인했습니다.");
    return true;
  } catch (error) {
    notify(error.message || "아직 확인할 내용이 남아 있습니다. 문항은 등록 상태로 계속 사용할 수 있습니다.", "error");
    return false;
  } finally {
    setBusy(false);
  }
}

async function approveAndNext() {
  if (state.dirty && !(await saveItem())) return;
  const approved = await approveItem();
  if (!approved) return;
  const nextId = nextReviewItemId();
  if (nextId) await selectItem(nextId, false);
  else notify("확인했습니다. 다음 확인 문항이 없습니다.");
}

function normalizeIssues(value) {
  const values = Array.isArray(value) ? value : (value ? [value] : []);
  return values.map((issue) => {
    const normalized = typeof issue === "string" ? { severity: "warning", message: issue, path: "" } : {
      severity: String(issue?.severity ?? issue?.level ?? "error").toLowerCase(),
      message: String(issue?.message ?? issue?.error ?? issue?.detail ?? "확인할 수 없는 오류"),
      path: String(issue?.path ?? issue?.location ?? issue?.field ?? "")
    };
    normalized.code = String(issue?.code ?? issue?.error_code ?? "");
    normalized.technical = normalized.message;
    const message = normalized.message;
    if (/^(OCR warning:|Ignored source region:|Pruned \d+|Filtered \d+|Filtered sibling |solution_link:|quick_answer:|box role |Ignored empty OCR box)/i.test(message)) return null;
    if (message.startsWith("HWP equation review")) return { ...normalized, message: "일부 수식을 확인해 주세요.", path: "" };
    if (message.startsWith("Visual QA")) return { ...normalized, message: "그림이나 배치를 확인해 주세요.", path: "" };
    if (message.includes("�")) return null;
    return normalized;
  }).filter(Boolean);
}

function issuesFromValidation(payload) {
  if (Array.isArray(payload)) return normalizeIssues(payload);
  const source = payload?.issues ?? payload?.errors ?? payload?.data?.issues ?? payload?.data?.errors ?? [];
  const warnings = payload?.warnings ?? payload?.data?.warnings ?? [];
  return [...normalizeIssues(source), ...normalizeIssues(warnings).map((issue) => ({ ...issue, severity: "warning" }))];
}

function renderIssues() {
  clearChildren(elements.issueList);
  elements.issuesEmpty.hidden = state.issues.length > 0;
  elements.issuesPanel.hidden = state.issues.length === 0;
  elements.issueSummary.textContent = state.issues.length ? `${state.issues.length}개` : "";
  for (const issue of state.issues) {
    const li = document.createElement("li");
    li.className = "issue";
    li.dataset.severity = ["error", "warning", "info"].includes(issue.severity) ? issue.severity : "error";
    const notice = pipelineNotice({ code: issue.code, message: issue.technical || issue.message });
    li.append(document.createTextNode(notice.target === "inspect" ? issue.message : notice.message));
    const label = { source: "원본 비교", equation: "수식 편집 열기", solution: "해설 확인", "figure-review": "그림 검수 열기", text: "현재 내용 다시 인식", reload: "다시 불러오기" }[notice.target];
    if (label) {
      const action = textNode("button", label, "text-button");
      action.type = "button";
      action.addEventListener("click", () => {
        if (notice.target === "reload") { elements.reload.click(); return; }
        if (notice.target === "text") { elements.transcribe.click(); return; }
        setSection(notice.target === "solution" || String(issue.path).includes("explanation") ? "explanation" : "question");
        let node = state.package;
        let blockId = null;
        for (const key of String(issue.path).replace(/^\$\.?/, "").match(/[^.[\]]+/g) || []) {
          node = node?.[key];
          if (node?.block_id) blockId = node.block_id;
        }
        if (blockId) selectReviewBlock(blockId, state.section);
        const destination = notice.target === "equation" ? elements.contentView : notice.target === "figure-review" ? elements.figureReviewPanel : elements.sourceView;
        destination.scrollIntoView({ block: "center" });
        destination.setAttribute("tabindex", "-1");
        destination.focus({ preventScroll: true });
      });
      li.append(action);
    }
    const technical = document.createElement("details");
    technical.className = "technical-info";
    technical.append(textNode("summary", "기술 정보"), textNode("p", `${issue.path || ""} ${issue.code || ""} ${issue.technical || issue.message}`));
    li.append(technical);
    elements.issueList.append(li);
  }
}

async function saveItem() {
  if (state.busy || !state.package) return false;
  if (requireFinishedRecrop("내용을 저장")) return false;
  const packageData = parseEditor();
  if (!packageData) {
    elements.editor.focus();
    notify("기술 정보의 형식 오류를 먼저 수정해 주세요.", "error");
    return false;
  }
  setBusy(true);
  try {
    const payload = await saveDualViewDraft((body) => api(endpoint("projects", state.projectId, "items", state.itemId), {
      method: "PATCH", body
    }), state.revision, packageData);
    const saved = detailFrom(payload?.package || payload?.document || payload?.item || payload?.data || payload?.item_id ? payload : {
      package: packageData,
      revision: state.revision + 1,
      asset_urls: state.assetUrls
    });
    if (!Object.keys(saved.assetUrls).length) saved.assetUrls = state.assetUrls;
    applyDetail(saved);
    notify("저장했습니다.");
    return true;
  } catch (error) {
    if (error.status === 409) {
      elements.editorError.textContent = "다른 작업에서 문항이 변경되었습니다. 다시 불러온 뒤 수정 내용을 적용하세요.";
      notify("저장 충돌이 발생했습니다. 자동 덮어쓰지 않았습니다.", "error");
    } else {
      elements.editorError.textContent = error.message || "저장하지 못했습니다.";
      notify(elements.editorError.textContent, "error");
    }
    return false;
  } finally {
    setBusy(false);
  }
}

function updateItemFromTagDetail(id, detail) {
  const index = state.items.findIndex((item) => itemId(item) === id);
  if (index < 0) return;
  state.items[index] = {
    ...state.items[index],
    identity: detail.package?.identity ?? state.items[index]?.identity,
    workflow: detail.package?.workflow ?? state.items[index]?.workflow,
    registration: detail.registration ?? state.items[index]?.registration,
    solution_link: detail.solutionLink ?? state.items[index]?.solution_link,
    tags: detail.tags,
    issue_count: detail.issues?.length ?? state.items[index]?.issue_count
  };
}

async function saveItemTags(event) {
  event.preventDefault();
  if (state.busy || !state.package) return;
  if (hasUnsavedWork()) {
    notify("문항 내용을 먼저 저장한 뒤 태그를 수정해 주세요.", "error");
    return;
  }
  let tags;
  try {
    tags = editedTags();
  } catch (error) {
    notify(error.message || "태그 값을 확인해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "tags"), {
      method: "PATCH",
      body: { expected_revision: state.revision, tags }
    });
    applyDetail(detailFrom(payload));
    notify("태그를 저장했습니다.");
  } catch (error) {
    notify(error.status === 409 ? "문항이 변경되었습니다. 다시 불러온 뒤 태그를 저장해 주세요."
      : error.message || "태그를 저장하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function autoTagCurrentItem() {
  if (state.busy || !state.package) return;
  if (hasUnsavedWork()) {
    notify("문항 내용을 먼저 저장한 뒤 자동 태깅을 실행해 주세요.", "error");
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "tag"), {
      method: "POST",
      body: { expected_revision: state.revision }
    });
    applyDetail(detailFrom(payload));
    notify(payload?.tagging_warning ? "자동 태깅을 마쳤습니다. 일부 태그는 확인이 필요합니다." : "자동 태깅을 마쳤습니다.");
  } catch (error) {
    notify(error.message || "자동 태깅을 완료하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function autoTagAllItems() {
  if (state.busy || !state.projectId || !state.items.length) return;
  if (hasUnsavedWork()) {
    notify("편집 중인 문항을 먼저 저장해 주세요.", "error");
    return;
  }
  const projectIdAtStart = state.projectId;
  const ids = state.items.map(itemId).filter(Boolean);
  let succeeded = 0;
  let failed = 0;
  state.taggingAll = true;
  elements.tagAllProgress.hidden = false;
  setBusy(true);
  try {
    for (let index = 0; index < ids.length; index += 1) {
      const id = ids[index];
      const summary = state.items.find((item) => itemId(item) === id);
      const revision = Number(summary?.workflow?.revision ?? 0);
      elements.tagAllProgress.textContent = `${index + 1}/${ids.length} · ${summary?.identity?.printed_number || "문항"} 분석 중`;
      try {
        const payload = await api(endpoint("projects", projectIdAtStart, "items", id, "tag"), {
          method: "POST",
          body: { expected_revision: revision }
        });
        const detail = detailFrom(payload);
        updateItemFromTagDetail(id, detail);
        if (id === state.itemId) applyDetail(detail);
        succeeded += 1;
      } catch {
        failed += 1;
      }
      renderItems();
    }
    const payload = await api(endpoint("projects", projectIdAtStart, "items"));
    if (state.projectId === projectIdAtStart) {
      state.items = asArray(payload, "items");
      renderItems();
    }
    elements.tagAllProgress.textContent = `${ids.length}/${ids.length} 완료 · 성공 ${succeeded}${failed ? ` · 확인 필요 ${failed}` : ""}`;
    notify(failed ? `자동 태깅을 마쳤습니다. ${failed}개 문항은 다시 확인해 주세요.` : "전체 문항 자동 태깅을 마쳤습니다.", failed ? "error" : "info");
  } catch (error) {
    elements.tagAllProgress.textContent = `자동 태깅 중단 · 성공 ${succeeded} · 확인 필요 ${failed}`;
    notify(error.message || "전체 자동 태깅을 마치지 못했습니다.", "error");
  } finally {
    state.taggingAll = false;
    setBusy(false);
  }
}

function bankRefKey(value) {
  const reference = value?.ref && typeof value.ref === "object" ? value.ref
    : value?.reference && typeof value.reference === "object" ? value.reference : value;
  return `${String(reference?.project_id || "")}:${String(reference?.item_id || "")}`;
}

function clearProjectBankSelections(projectIdValue) {
  const prefix = `${projectIdValue}:`;
  state.bankSelection = new Set([...state.bankSelection].filter((key) => !key.startsWith(prefix)));
  saveBankSelection();
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const storageKey = window.localStorage.key(index);
      if (!storageKey?.startsWith("bank-selection:")) continue;
      const references = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(references)) {
        window.localStorage.setItem(storageKey, JSON.stringify(references.filter((ref) => ref?.project_id !== projectIdValue)));
      }
    }
  } catch {}
}

function questionSetFrom(payload) {
  const value = payload?.question_set ?? payload?.set ?? payload?.data ?? payload;
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function questionSetId(value) {
  return String(value?.set_id ?? value?.id ?? "");
}

function questionSetItems(value = state.activeSet) {
  const items = value?.items ?? value?.ordered_items;
  return Array.isArray(items) ? items : [];
}

function bankItemTags(item) {
  return tagValues(tagsOf(item?.summary ?? item));
}

function bankItemPreview(item) {
  const value = item?.summary ?? item;
  const preview = String(value?.preview_text ?? value?.preview?.text ?? value?.question_preview ?? "").trim();
  return preview || "원본 이미지로 저장된 문항";
}

function bankItemSource(item) {
  const value = item?.summary ?? item;
  const source = value?.source ?? {};
  const reference = value?.ref ?? value?.reference ?? value;
  const title = String(source.project_title ?? value?.source_title ?? value?.project_title ?? value?.book_title ?? "");
  return title && title !== String(reference?.project_id ?? "") ? title : "출처 미분류";
}

function bankItemFilename(item) {
  const value = item?.summary ?? item;
  return String(value?.source?.original_filename ?? "");
}

function bankItemSourceKey(item) {
  const value = item?.summary ?? item;
  const reference = value?.ref ?? value?.reference ?? value;
  const title = String(value?.source?.project_title ?? value?.source_title ?? value?.project_title ?? value?.book_title ?? "");
  return title && title !== String(reference?.project_id ?? "") ? title : bankItemFilename(item);
}

function bankItemNumber(item) {
  const value = item?.summary ?? item;
  return String(value?.identity?.printed_number ?? value?.preview?.printed_number ?? value?.printed_number ?? "문항");
}

function bankItemDependency(item) {
  const value = item?.summary ?? item;
  const dependency = value?.dependency;
  return dependency && typeof dependency === "object" && dependency.shared_context === true
    ? dependency : null;
}

function bankDuplicateWarning(item) {
  const value = item?.summary ?? item;
  const warning = value?.duplicate_warning ?? value?.duplicate;
  return warning && typeof warning === "object" ? warning : null;
}

function setBankBusy(busy) {
  state.bankBusy = busy;
  state.setBusy = busy;
  [elements.refreshBank, elements.createSetFromBank, elements.createQuestionSet,
    elements.saveQuestionSet, elements.buildQuestionSetHwpx, elements.regenerateSelectedFigures]
    .forEach((control) => { if (control) control.disabled = busy; });
  elements.bankList.querySelectorAll('input[type="checkbox"][data-bank-selection]').forEach((control) => {
    control.disabled = busy;
  });
  elements.bankView.setAttribute("aria-busy", String(busy));
  elements.setWorkspace.setAttribute("aria-busy", String(busy));
  syncBankFigureSelectionUi();
  syncBankQuestionSelectionUi();
  if (!busy && state.activeSet) renderActiveQuestionSet();
}

function renderBankFilterOptions() {
  const values = Object.fromEntries(Object.keys(BANK_FILTERS).map((key) => [key, new Map()]));
  for (const item of state.bankItems) {
    const tags = bankItemTags(item);
    const add = (key, list, label = (value) => value) => list.forEach((value) => {
      if (value) values[key].set(value, label(value));
    });
    add("subject", [tags.subject]);
    add("unit", [tags.unit]);
    const sourceKey = bankItemSourceKey(item);
    add("source", [sourceKey], () => [bankItemSource(item), bankItemFilename(item)].filter(Boolean).join(" · "));
    add("difficulty", [tags.difficulty].filter((value) => value && value !== "unknown"), (value) => DIFFICULTY_LABELS[value] ?? value);
    add("problemTypes", tags.problemTypes, (value) => PROBLEM_TYPE_LABELS[value] ?? value);
    add("keywords", tags.keywords);
    add("features", Object.entries(tags.features).filter(([, enabled]) => enabled === true).map(([key]) => key.replace(/^has_/, "")), (value) => FEATURE_LABELS[`has_${value}`] ?? value);
    const figure = figureGenerationOf(item);
    add("figureStatus", [
      figure.regeneratedCount > 0 && "has_regenerated",
      figure.missingRegenerationCount > 0 && "missing_regeneration",
      figure.selectedRegeneratedCount > 0 && "selected_regenerated",
      (figure.reviewPendingCount > 0 || figure.blockedMixedCount > 0) && "needs_review",
      figure.figureCount === 0 && "no_figure"
    ].filter(Boolean), (value) => FIGURE_STATUS_LABELS[value] ?? value);
  }
  const stateSearch = activeBankSearch(state.bankSearch);
  for (const [category, select] of Object.entries(BANK_FILTERS)) {
    const available = [...values[category]].sort(([left], [right]) => left.localeCompare(right, "ko-KR"));
    const selected = new Set(stateSearch.filters[category] ?? []);
    select.replaceChildren(new Option("미분류", "__unclassified"), ...available.map(([key, label]) => new Option(label, key)));
    for (const option of select.options) option.selected = selected.has(option.value);
    stateSearch.filters[category] = [...selected].filter((value) => value === "__unclassified" || values[category].has(value));
  }
}

function filteredBankItems() {
  const search = activeBankSearch(state.bankSearch);
  const items = state.bankItems.filter((item) => !state.bankPending.has(bankRefKey(item))).map((item, originalIndex) => {
    const tags = bankItemTags(item);
    const features = tags.features || {};
    const source = item?.summary?.source ?? item?.source ?? {};
    const figure = figureGenerationOf(item);
    const categories = {
      subject: tags.subject ? [tags.subject] : [], unit: tags.unit ? [tags.unit] : [],
      source: bankItemSourceKey(item) ? [bankItemSourceKey(item)] : [],
      difficulty: tags.difficulty && tags.difficulty !== "unknown" ? [tags.difficulty] : [],
      problemTypes: tags.problemTypes, keywords: tags.keywords,
      features: Object.entries(features).filter(([, enabled]) => enabled === true).map(([key]) => key.replace(/^has_/, "")),
      figureStatus: [
        figure.regeneratedCount > 0 && "has_regenerated",
        figure.missingRegenerationCount > 0 && "missing_regeneration",
        figure.selectedRegeneratedCount > 0 && "selected_regenerated",
        (figure.reviewPendingCount > 0 || figure.blockedMixedCount > 0) && "needs_review",
        figure.figureCount === 0 && "no_figure"
      ].filter(Boolean)
    };
    const reference = item?.ref ?? item?.reference ?? item;
    return {
      ...item, originalIndex, refKey: bankRefKey(item), printedNumber: bankItemNumber(item),
      updatedAt: item?.updated_at ?? item?.summary?.updated_at ?? null,
      categories, source, searchText: [bankItemNumber(item), bankItemPreview(item), bankItemSource(item), bankItemFilename(item), tagSearchText(item)].join(" ")
    };
  });
  return filterAndSortBankItems(items, search);
}

function bankSearchStorageKeyFor(items) {
  const projects = [...new Set(items.map((item) => String((item?.ref ?? item?.reference ?? item)?.project_id ?? "")).filter(Boolean))].sort();
  let hash = 2166136261;
  for (const character of projects.join("\0")) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  return `bank-search:${window.location.origin}:${(hash >>> 0).toString(16)}`;
}

function saveBankSearch() {
  if (!bankSearchStorageKey) return;
  try { saveBankSearchState(window.localStorage, bankSearchStorageKey, state.bankSearch); } catch { /* Storage may be disabled. */ }
}

function saveBankSelection() {
  if (!bankSelectionStorageKey) return;
  const references = state.bankItems.filter((item) => state.bankSelection.has(bankRefKey(item))).map((item) => {
    const value = item?.ref ?? item?.reference ?? item;
    return { project_id: String(value.project_id), item_id: String(value.item_id) };
  });
  try { window.localStorage.setItem(bankSelectionStorageKey, JSON.stringify(references)); } catch {}
}

function loadBankSelection(key) {
  try {
    const values = JSON.parse(window.localStorage.getItem(key) || "[]");
    return new Set(Array.isArray(values) ? values.filter((value) => typeof value?.project_id === "string" && value.project_id
      && typeof value?.item_id === "string" && value.item_id).map(bankRefKey) : []);
  } catch { return new Set(); }
}

function syncBankSearchControls() {
  const search = activeBankSearch(state.bankSearch);
  elements.bankSearch.value = search.query;
  elements.bankNumberSearch.value = search.numberQuery;
  elements.bankSort.value = search.sort;
  elements.bankPageSize.value = String(search.pageSize);
  for (const [category, select] of Object.entries(BANK_FILTERS)) {
    const selected = new Set(search.filters[category] ?? []);
    for (const option of select.options) option.selected = selected.has(option.value);
  }
}

function renderBankSearchTabs() {
  clearChildren(elements.bankSearchTabs);
  for (const tab of state.bankSearch.tabs) {
    const wrapper = document.createElement("div");
    wrapper.className = "bank-search-tab";
    const button = textNode("button", tab.label, "bank-search-tab-button");
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab.id === state.bankSearch.activeTabId));
    button.addEventListener("click", () => {
      updateBankSearch(state.bankSearch, { scrollY: window.scrollY });
      saveBankSearch();
      selectBankSearch(state.bankSearch, tab.id);
      syncBankSearchControls();
      renderBankItems();
      window.scrollTo(0, activeBankSearch(state.bankSearch).scrollY);
    });
    const close = textNode("button", "×", "bank-search-tab-close");
    close.type = "button";
    close.disabled = state.bankSearch.tabs.length === 1;
    close.setAttribute("aria-label", `${tab.label} 닫기`);
    close.addEventListener("click", () => {
      updateBankSearch(state.bankSearch, { scrollY: window.scrollY });
      closeBankSearch(state.bankSearch, tab.id);
      bankGroupExpansionByTab.delete(tab.id);
      syncBankSearchControls();
      renderBankItems();
      saveBankSearch();
      window.scrollTo(0, activeBankSearch(state.bankSearch).scrollY);
    });
    wrapper.append(button, close);
    elements.bankSearchTabs.append(wrapper);
  }
}

function renderBankConditionChips() {
  clearChildren(elements.bankConditionChips);
  const search = activeBankSearch(state.bankSearch);
  const labels = { subject: "과목", unit: "단원", source: "출처", difficulty: "난이도", problemTypes: "유형", keywords: "핵심어", features: "특징", figureStatus: "그림 상태" };
  const add = (label, remove) => {
    const chip = textNode("button", `${label} ×`, "bank-condition-chip");
    chip.type = "button";
    chip.setAttribute("aria-label", `${label} 조건 해제`);
    chip.addEventListener("click", remove);
    elements.bankConditionChips.append(chip);
  };
  if (search.query) add(`키워드: ${search.query}`, () => changeBankSearch({ query: "" }));
  if (search.numberQuery) add(`원본 번호: ${search.numberQuery}`, () => changeBankSearch({ numberQuery: "" }));
  for (const [category, selected] of Object.entries(search.filters)) {
    for (const value of selected) {
      const option = [...BANK_FILTERS[category].options].find((item) => item.value === value);
      add(`${labels[category]}: ${option?.textContent ?? value}`, () => {
        changeBankSearch({ filters: { ...search.filters, [category]: selected.filter((item) => item !== value) } });
        syncBankSearchControls();
      });
    }
  }
  elements.bankConditionChips.hidden = !elements.bankConditionChips.childElementCount;
  elements.resetBankSearch.disabled = !search.query && !search.numberQuery && !Object.values(search.filters).some((values) => values.length);
}

function changeBankSearch(patch, resetPage = true) {
  updateBankSearch(state.bankSearch, { ...patch, page: resetPage ? 1 : patch.page, scrollY: 0 });
  renderBankItems();
  saveBankSearch();
}

function readBankSearchControls() {
  const filters = Object.fromEntries(Object.entries(BANK_FILTERS).map(([category, select]) => [
    category, [...select.selectedOptions].map((option) => option.value)
  ]));
  const query = elements.bankSearch.value.trim();
  if (/AS1-[a-f\d]{64}/i.test(query) || /AS1-[a-f\d]{64}/i.test(elements.bankNumberSearch.value)) {
    elements.bankSearch.value = "";
    elements.bankNumberSearch.value = "";
    changeBankSearch({ query: "", numberQuery: "" });
    notify("내부 관리 코드는 검색할 수 없습니다.", "error");
    return;
  }
  changeBankSearch({ query, numberQuery: elements.bankNumberSearch.value.trim(), filters });
}

function renderBankTargetSets() {
  const current = state.activeSetId || elements.bankTargetSet.value;
  elements.bankTargetSet.replaceChildren();
  if (!state.questionSets.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "먼저 새 문제지를 만드세요";
    elements.bankTargetSet.append(option);
  } else {
    state.questionSets.forEach((value, index) => {
      const option = document.createElement("option");
      option.value = questionSetId(value);
      option.textContent = String(value?.title || `문제지 ${index + 1}`);
      elements.bankTargetSet.append(option);
    });
    elements.bankTargetSet.value = state.questionSets.some((value) => questionSetId(value) === current)
      ? current : questionSetId(state.questionSets[0]);
  }
}

function expandedFilteredBankItems() {
  const expanded = new Map();
  for (const item of filteredBankItems()) {
    const dependency = bankItemDependency(item);
    const siblings = dependency?.group_key
      ? state.bankItems.filter((candidate) => bankItemDependency(candidate)?.group_key === dependency.group_key)
      : [item];
    siblings.forEach((candidate) => expanded.set(bankRefKey(candidate), candidate));
  }
  return [...expanded.values()];
}

function bankSelectionSavePlan(selectedKeys = state.bankSelection) {
  const chosen = state.bankItems.filter((item) => selectedKeys.has(bankRefKey(item)));
  const expanded = new Map();
  for (const item of chosen) {
    const dependency = bankItemDependency(item);
    const siblings = dependency?.group_key
      ? state.bankItems.filter((candidate) => bankItemDependency(candidate)?.group_key === dependency.group_key)
        .sort((left, right) => Number(bankItemDependency(left)?.sub_index || 0) - Number(bankItemDependency(right)?.sub_index || 0))
      : [item];
    siblings.forEach((candidate) => expanded.set(bankRefKey(candidate), candidate));
  }
  const existing = state.activeSetId === elements.bankTargetSet.value
    ? new Set(questionSetItems().map(bankRefKey)) : new Set();
  const items = [...expanded.values()].filter((item) => !existing.has(bankRefKey(item)));
  return { items, extraCount: items.filter((item) => !selectedKeys.has(bankRefKey(item))).length };
}

function currentBankPageItems() {
  const search = activeBankSearch(state.bankSearch);
  return paginateBankItems(filteredBankItems(), search.page, search.pageSize).items;
}

function selectBankPageItems(limit = Infinity) {
  currentBankPageItems().filter((item) => {
    const key = bankRefKey(item);
    return !state.bankSelection.has(key) && !state.bankPending.has(key);
  }).slice(0, limit).forEach((item) => state.bankSelection.add(bankRefKey(item)));
  saveBankSelection();
  syncBankQuestionSelectionUi();
}

function selectedBankFigureItems() {
  return state.bankItems.filter(
    (item) => bankFigureGenerationAllowed(item)
      && state.bankSelection.has(bankRefKey(item)) && !state.bankPending.has(bankRefKey(item))
  );
}

const BANK_MOVE_KEY = "bank-pending-move";

function bankMoveStatus(message) {
  elements.bankSelectionStatus.dataset.result = message ? "true" : "";
  elements.bankSelectionStatus.textContent = message;
}

function finishBankMove(operation, record) {
  const results = [...(Array.isArray(record?.results) ? record.results : []), ...(operation.previewFailed || [])];
  const outcomes = new Map(results.map((entry) => [`${entry.project_id}:${entry.item_id}`, entry]));
  let deleted = 0;
  let failed = 0;
  for (const key of operation.keys) {
    if (!state.bankPending.has(key)) continue;
    const result = outcomes.get(key);
    if (!result && record?.status !== "done") continue;
    state.bankPending.delete(key);
    if (result?.status === "deleted") {
      deleted += 1;
      state.bankSelection.delete(key);
      state.bankItems = state.bankItems.filter((item) => bankRefKey(item) !== key);
      const project = state.projects.find((value) => projectId(value) === key.split(":", 1)[0]);
      if (project && Number.isInteger(project.item_count)) project.item_count = Math.max(0, project.item_count - 1);
    } else {
      failed += 1;
      if (operation.selected.includes(key)) state.bankSelection.add(key);
    }
  }
  saveBankSelection();
  renderBankItems();
  if (record?.status === "done") {
    window.sessionStorage.removeItem(BANK_MOVE_KEY);
    const reason = results.find((entry) => entry.status === "failed")?.reason;
    bankMoveStatus(failed ? `${deleted}개 이동, ${failed}개 실패. ${reason || "선택을 유지했습니다."}` : "");
  } else {
    bankMoveStatus(`${state.bankPending.size}개 삭제 상태 확인 중`);
  }
}

async function recoverBankMove(operation) {
  try {
    const record = await api(endpoint("trash", "move-items-status"), {
      method: "POST", body: { operation_id: operation.operation_id }
    });
    finishBankMove(operation, record);
    if (record.status !== "done") window.setTimeout(() => recoverBankMove(operation), 1500);
  } catch (error) {
    bankMoveStatus("삭제 결과를 확인하지 못했습니다. 다시 열어 상태를 확인하세요.");
  }
}

async function moveSelectedBankItems() {
  const keys = [...state.bankSelection].filter((key) => !state.bankPending.has(key));
  if (!keys.length) return;
  const items = state.bankItems.filter((item) => keys.includes(bankRefKey(item))).map((item) => {
    const ref = item?.ref ?? item?.reference ?? item;
    return { project_id: String(ref.project_id), item_id: String(ref.item_id) };
  });
  if (!items.length) return;
  const operation = { operation_id: `move-${crypto.randomUUID()}`, keys: items.map(bankRefKey), selected: keys };
  operation.keys.forEach((key) => { state.bankPending.add(key); state.bankSelection.delete(key); });
  window.sessionStorage.setItem(BANK_MOVE_KEY, JSON.stringify(operation));
  saveBankSelection();
  renderBankItems();
  bankMoveStatus(`${operation.keys.length}개 삭제 중`);
  let preview;
  try {
    preview = await api(endpoint("trash", "move-items-preview"), { method: "POST", body: { items } });
  } catch (error) {
    finishBankMove(operation, { status: "done", results: operation.keys.map((key) => {
      const [project_id, item_id] = key.split(":");
      return { project_id, item_id, status: "failed" };
    }) });
    bankMoveStatus(error.message || "삭제 대상을 확인하지 못했습니다.");
    return;
  }
  operation.previewFailed = preview.failed || [];
  window.sessionStorage.setItem(BANK_MOVE_KEY, JSON.stringify(operation));
  if (!preview.items.length) {
    finishBankMove(operation, { status: "done", results: [] });
    return;
  }
  try {
    const record = await api(endpoint("trash", "move-items"), {
      method: "POST", body: { operation_id: operation.operation_id,
        expected_trash_revision: preview.trash_revision, items: preview.items }
    });
    finishBankMove(operation, record);
  } catch (error) {
    await recoverBankMove(operation);
  }
}

function bankFigureGenerationAllowed(item) {
  return String(item?.provider_profile ?? "full") !== "codex_only";
}

function syncBankFigureSelectionUi() {
  const count = selectedBankFigureItems().length;
  elements.regenerateSelectedFigures.disabled = state.bankBusy || count === 0;
  elements.regenerateSelectedFigures.title = count ? "선택한 문항의 그림을 재생성합니다."
    : "그림 재생성 대상을 먼저 선택해 주세요.";
}

function renderBankItems() {
  clearChildren(elements.bankList);
  const matchingItems = filteredBankItems();
  const search = activeBankSearch(state.bankSearch);
  const page = paginateBankItems(matchingItems, search.page, search.pageSize);
  if (page.page !== search.page) {
    updateBankSearch(state.bankSearch, { page: page.page });
    saveBankSearch();
  }
  const items = page.items;
  renderBankSearchTabs();
  renderBankConditionChips();
  elements.bankCount.textContent = String(page.total);
  elements.bankRange.textContent = page.total ? `${page.start + 1}–${page.end} / ${page.total}개` : "0개";
  elements.bankPageStatus.textContent = `${page.page} / ${page.pageCount}쪽`;
  elements.bankPreviousPage.disabled = page.page <= 1;
  elements.bankNextPage.disabled = page.page >= page.pageCount;
  elements.bankLoadStatus.hidden = !state.bankLoadError;
  elements.bankLoadStatus.textContent = state.bankLoadError
    ? (state.bankItems.length ? "새로고침에 실패했습니다. 마지막으로 불러온 결과를 표시하고 있습니다." : "문제은행을 불러오지 못했습니다. 연결 상태를 확인한 뒤 다시 시도하세요.") : "";
  elements.bankEmpty.hidden = page.total > 0 || Boolean(state.bankLoadError);
  elements.bankEmpty.textContent = state.bankItems.length ? "조건에 맞는 문항이 없습니다." : "저장된 문항이 없습니다.";
  const matchingGroups = new Map();
  items.forEach((item) => {
    const dependency = bankItemDependency(item);
    if (!dependency?.group_key || matchingGroups.has(dependency.group_key)) return;
    matchingGroups.set(
      dependency.group_key,
      state.bankItems
        .filter((candidate) => bankItemDependency(candidate)?.group_key === dependency.group_key)
        .sort((left, right) => Number(bankItemDependency(left)?.sub_index || 0) - Number(bankItemDependency(right)?.sub_index || 0))
    );
  });
  const selected = new Set(questionSetItems().map(bankRefKey));
  const tabId = state.bankSearch.activeTabId;
  let pageItemCount = 0;
  const appendCard = (item, parent) => {
    const card = document.createElement("article");
    card.className = "bank-card";
    const key = bankRefKey(item);
    card.dataset.selected = String(selected.has(key));
    card.dataset.bankSelected = String(state.bankSelection.has(key));
    const heading = document.createElement("div");
    heading.className = "bank-card-heading";
    const headingCopy = document.createElement("div");
    headingCopy.append(textNode("strong", bankItemNumber(item)), textNode("span", bankItemSource(item), "bank-card-source"));
    if (bankItemFilename(item)) headingCopy.append(textNode("span", `원본 파일 · ${bankItemFilename(item)}`, "bank-card-file"));
    const selectLabel = document.createElement("label");
    selectLabel.className = "bank-card-select";
    const select = document.createElement("input");
    select.type = "checkbox";
    select.dataset.bankSelection = key;
    select.checked = state.bankSelection.has(key);
    select.disabled = state.bankBusy;
    select.setAttribute("aria-label", `${bankItemNumber(item)} 문항 선택`);
    select.addEventListener("change", () => {
      if (select.checked) state.bankSelection.add(key);
      else state.bankSelection.delete(key);
      saveBankSelection();
      syncBankQuestionSelectionUi();
    });
    selectLabel.append(select, textNode("span", "문항 선택"));
    heading.append(headingCopy, selectLabel);
    const images = document.createElement("div");
    images.className = "bank-card-images";
    const previewValue = item?.summary ?? item;
    const imageAssets = Array.isArray(previewValue?.preview?.images) ? previewValue.preview.images : [];
    const fallback = textNode("div", "", "bank-image-fallback");
    fallback.setAttribute("role", "status");
    fallback.append(textNode("strong", "원본 이미지 없음"), textNode("span", "문항 상세에서 확인하세요."));
    if (imageAssets.length) {
      const reference = previewValue?.ref ?? previewValue?.reference ?? previewValue;
      let failed = 0;
      imageAssets.forEach((asset, index) => {
        const frame = document.createElement("div");
        frame.className = "bank-image-frame";
        const image = document.createElement("img");
        image.src = endpoint("projects", reference.project_id, "items", reference.item_id, "assets", asset.asset_id);
        image.alt = `${bankItemNumber(item)} 원본 문제 이미지 ${index + 1}`;
        image.loading = "lazy";
        if (Number.isInteger(asset.width_px) && asset.width_px > 0) image.width = asset.width_px;
        if (Number.isInteger(asset.height_px) && asset.height_px > 0) image.height = asset.height_px;
        image.addEventListener("error", () => {
          frame.hidden = true;
          failed += 1;
          if (failed === imageAssets.length) fallback.hidden = false;
        }, { once: true });
        const link = document.createElement("a");
        link.href = `/?view=saved&project=${encodeURIComponent(reference.project_id)}&item=${encodeURIComponent(reference.item_id)}&mode=read`;
        link.addEventListener("click", (event) => {
          if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          openBankReadDialog(reference.project_id, reference.item_id);
        });
        link.append(image);
        frame.append(link);
        images.append(frame);
      });
      fallback.hidden = true;
    }
    images.append(fallback);
    const tagsNode = document.createElement("div");
    tagsNode.className = "bank-card-tags";
    const tags = bankItemTags(item);
    [tags.subject, tags.unit, DIFFICULTY_LABELS[tags.difficulty], ...tags.problemTypes.slice(0, 2).map((value) => PROBLEM_TYPE_LABELS[value] ?? value)]
      .filter(Boolean).forEach((value) => tagsNode.append(textNode("span", value)));
    if (bankDuplicateWarning(item)) tagsNode.append(textNode("span", "중복 가능", "duplicate-badge"));
    appendFigureGenerationBadges(tagsNode, item);
    card.append(heading, images, tagsNode);
    parent.append(card);
  };
  const emittedGroups = new Set();
  for (const item of items) {
    if (pageItemCount >= page.pageSize) break;
    const dependency = bankItemDependency(item);
    if (!dependency?.group_key) {
      appendCard(item, elements.bankList);
      pageItemCount += 1;
      continue;
    }
    if (emittedGroups.has(dependency.group_key)) continue;
    emittedGroups.add(dependency.group_key);
    const siblings = matchingGroups.get(dependency.group_key) || [item];
    const group = document.createElement("section");
    group.className = "bank-shared-group";
    group.dataset.groupKey = dependency.group_key;
    const groupHeading = document.createElement("header");
    groupHeading.className = "bank-shared-heading";
    const groupCopy = document.createElement("div");
    groupCopy.className = "bank-shared-copy";
    groupCopy.dataset.groupKey = dependency.group_key;
    const visibleKeys = new Set(items.map(bankRefKey));
    const visibleSiblings = siblings.filter((sibling) => visibleKeys.has(bankRefKey(sibling)))
      .slice(0, Math.max(0, page.pageSize - pageItemCount));
    if (!visibleSiblings.length) continue;
    const selectedCount = siblings.filter((sibling) => state.bankSelection.has(bankRefKey(sibling))).length;
    groupCopy.append(
      textNode("strong", `${dependency.group_label || "공통 문항"} 세트 · 소문항 ${siblings.length}개 · 선택 ${selectedCount}개`),
      textNode("span", `전체 ${siblings.length}개 중 이 페이지 ${visibleSiblings.length}개`)
    );
    const groupActions = document.createElement("div");
    groupActions.className = "bank-shared-actions";
    const addGroup = textNode("button", "세트 전체 선택", "button");
    addGroup.type = "button";
    addGroup.disabled = state.bankBusy;
    addGroup.addEventListener("click", () => {
      siblings.forEach((sibling) => state.bankSelection.add(bankRefKey(sibling)));
      saveBankSelection();
      syncBankQuestionSelectionUi();
    });
    groupActions.append(addGroup);
    const childGrid = document.createElement("div");
    childGrid.className = "bank-shared-items";
    childGrid.id = `bank-group-items-${encodeURIComponent(tabId)}-${encodeURIComponent(dependency.group_key)}`;
    const expanded = bankGroupIsExpanded(bankGroupExpansionByTab, tabId, dependency.group_key);
    childGrid.hidden = !expanded;
    childGrid.dataset.rendered = String(expanded);
    const toggle = textNode("button", expanded ? "접기" : "펼치기", "bank-group-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-controls", childGrid.id);
    toggle.addEventListener("click", () => {
      const nextExpanded = toggle.getAttribute("aria-expanded") !== "true";
      setBankGroupExpanded(bankGroupExpansionByTab, tabId, dependency.group_key, nextExpanded);
      toggle.setAttribute("aria-expanded", String(nextExpanded));
      toggle.textContent = nextExpanded ? "접기" : "펼치기";
      childGrid.hidden = !nextExpanded;
      if (nextExpanded && childGrid.dataset.rendered !== "true") {
        visibleSiblings.forEach((sibling) => appendCard(sibling, childGrid));
        childGrid.dataset.rendered = "true";
      }
    });
    groupActions.prepend(toggle);
    groupHeading.append(groupCopy, groupActions);
    if (expanded) visibleSiblings.forEach((sibling) => appendCard(sibling, childGrid));
    pageItemCount += visibleSiblings.length;
    group.append(groupHeading, childGrid);
    elements.bankList.append(group);
  }
  const similarityNotChecked = state.bankItems.some((item) => (item?.summary ?? item)?.duplicate_similarity_status === "not_checked");
  elements.bankRange.title = similarityNotChecked ? "유사도 미확인 문항이 포함되어 있습니다." : "";
  elements.bankRange.dataset.similarity = String(similarityNotChecked);
  elements.bankSimilarityNotice.hidden = !similarityNotChecked;
  syncBankQuestionSelectionUi();
  syncBankFigureSelectionUi();
}

function syncBankQuestionSelectionUi() {
  if (pendingBankFigurePlan?.selectionKeys !== undefined
      && pendingBankFigurePlan.selectionKeys !== [...state.bankSelection].sort().join("|")) {
    pendingBankFigurePlan = null;
    elements.regenerateSelectedFigures.textContent = "그림 재생성";
    bankMoveStatus("");
  }
  const count = state.bankSelection.size;
  elements.bankSelectionCount.textContent = `${count}개 선택`;
  const plan = bankSelectionSavePlan();
  elements.selectBankPage.disabled = state.bankBusy || !currentBankPageItems().some((item) => {
    const key = bankRefKey(item);
    return !state.bankSelection.has(key) && !state.bankPending.has(key);
  });
  document.querySelectorAll("[data-bank-quick-select]").forEach((button) => {
    const limit = Number(button.dataset.bankQuickSelect);
    const eligible = currentBankPageItems().filter((item) => {
      const key = bankRefKey(item);
      return !state.bankSelection.has(key) && !state.bankPending.has(key);
    }).length;
    button.disabled = state.bankBusy || eligible === 0;
    button.title = `현재 페이지에서 최대 ${limit}개 추가 선택`;
  });
  elements.addSelectedBankItems.disabled = state.bankBusy || !count || !elements.bankTargetSet.value || !plan.items.length;
  elements.deleteSelectedBankItems.disabled = count === 0;
  elements.clearBankSelection.disabled = state.bankBusy || count === 0;
  if (!state.bankPending.size && !elements.bankSelectionStatus.dataset.result) elements.bankSelectionStatus.textContent = count && plan.extraCount ? `묶음 문항 ${plan.extraCount}개를 함께 담습니다.` : "";
  elements.bankList.querySelectorAll(".bank-shared-copy[data-group-key]").forEach((copy) => {
    const groupItems = state.bankItems.filter((item) => bankItemDependency(item)?.group_key === copy.dataset.groupKey);
    const selectedCount = groupItems.filter((item) => state.bankSelection.has(bankRefKey(item))).length;
    const label = bankItemDependency(groupItems[0])?.group_label || "공통 문항";
    copy.querySelector("strong").textContent = `${label} 세트 · 소문항 ${groupItems.length}개 · 선택 ${selectedCount}개`;
  });
  elements.bankList.querySelectorAll("input[data-bank-selection]").forEach((checkbox) => {
    const selected = state.bankSelection.has(checkbox.dataset.bankSelection);
    checkbox.checked = selected;
    checkbox.disabled = state.bankBusy || state.bankPending.has(checkbox.dataset.bankSelection);
    const card = checkbox.closest(".bank-card");
    if (card) card.dataset.bankSelected = String(selected);
  });
  syncBankFigureSelectionUi();
}

async function loadBankItems() {
  const request = bankLoadRequests.begin();
  setBankBusy(true);
  try {
    const payload = await api(endpoint("bank", "items"));
    if (!bankLoadRequests.isCurrent(request)) return;
    const bankItems = asArray(payload, "items");
    const storageKey = bankSearchStorageKeyFor(bankItems);
    if (storageKey !== bankSearchStorageKey) {
      bankSearchStorageKey = storageKey;
      bankSelectionStorageKey = `bank-selection:${storageKey}`;
      try { state.bankSearch = loadBankSearchState(window.localStorage, storageKey); }
      catch { state.bankSearch = createBankSearchState(); }
      state.bankSelection = loadBankSelection(bankSelectionStorageKey);
    }
    state.bankItems = bankItems;
    state.bankLoadError = null;
    const available = new Set(state.bankItems.map(bankRefKey));
    state.bankSelection = new Set([...state.bankSelection].filter((key) => available.has(key)));
    saveBankSelection();
    renderBankFilterOptions();
    syncBankSearchControls();
    renderBankItems();
  } catch (error) {
    if (bankLoadRequests.isCurrent(request)) {
      state.bankLoadError = error.message || "연결 상태를 확인한 뒤 다시 시도해 주세요.";
      renderBankItems();
    }
  } finally {
    if (bankLoadRequests.isCurrent(request)) setBankBusy(false);
  }
}

function renderQuestionSetList() {
  clearChildren(elements.questionSetList);
  elements.setsEmpty.hidden = state.questionSets.length > 0;
  state.questionSets.forEach((value, index) => {
    const li = document.createElement("li");
    const button = textNode("button", String(value?.title || `문제지 ${index + 1}`), "nav-button");
    button.type = "button";
    button.setAttribute("aria-current", String(questionSetId(value) === state.activeSetId));
    button.addEventListener("click", () => selectQuestionSet(questionSetId(value)));
    li.append(button);
    elements.questionSetList.append(li);
  });
  elements.bankSavedSetLink.textContent = `내 문제지 ${state.questionSets.length}개`;
  renderBankTargetSets();
}

async function loadQuestionSets(preferredId = null) {
  try {
    const payload = await api(endpoint("question-sets"));
    state.questionSets = asArray(payload, "question_sets").length
      ? asArray(payload, "question_sets") : asArray(payload, "sets");
    renderQuestionSetList();
    const wanted = preferredId || state.activeSetId;
    if (wanted && state.questionSets.some((value) => questionSetId(value) === wanted)) {
      await selectQuestionSet(wanted);
    } else if (["bank", "sets"].includes(state.view) && state.questionSets.length) {
      await selectQuestionSet(questionSetId(state.questionSets[0]));
    }
  } catch (error) {
    state.questionSets = [];
    renderQuestionSetList();
    notify(error.message || "문제지 목록을 불러오지 못했습니다.", "error");
  }
}

function setResolvedSummary(reference) {
  const resolved = state.activeSet?.resolved_items ?? state.activeSet?.resolved ?? [];
  return resolved.find((value) => bankRefKey(value) === bankRefKey(reference))
    ?? state.bankItems.find((value) => bankRefKey(value) === bankRefKey(reference))
    ?? null;
}

function setItemDependency(reference) {
  return bankItemDependency(setResolvedSummary(reference));
}

function setGroupRangeAt(items, index) {
  const dependency = setItemDependency(items[index]);
  if (!dependency?.group_key) return { start: index, end: index, key: null, label: null };
  let start = index;
  let end = index;
  while (start > 0 && setItemDependency(items[start - 1])?.group_key === dependency.group_key) start -= 1;
  while (end + 1 < items.length && setItemDependency(items[end + 1])?.group_key === dependency.group_key) end += 1;
  return { start, end, key: dependency.group_key, label: dependency.group_label || "공통 문항" };
}

function documentCreationPayload(value) {
  if (!value?.set_id || !Number.isInteger(value.revision) || value.revision < 1 || !Array.isArray(value.items)) {
    throw new Error("저장된 문제지와 버전을 확인해 주세요.");
  }
  return { set_id: value.set_id, expected_revision: value.revision,
    items: value.items.map(({ project_id, item_id }) => ({ project_id, item_id })) };
}

function renderActiveQuestionSet() {
  const value = state.activeSet;
  elements.setWelcome.hidden = Boolean(value);
  elements.setEditor.hidden = !value;
  if (!value) return;
  elements.createDocument.disabled = state.setBusy || state.setDirty || !value.items.length;
  elements.documentEntryPayload.textContent = JSON.stringify(documentCreationPayload(value), null, 2);
  elements.setTitle.value = String(value.title || "새 문제지");
  const mode = String(value.output_mode || "separate");
  document.querySelectorAll('input[name="setOutputMode"]').forEach((input) => { input.checked = input.value === mode; });
  const items = questionSetItems(value);
  elements.setItemCount.textContent = String(items.length);
  elements.setItemsEmpty.hidden = items.length > 0;
  clearChildren(elements.setItemList);
  items.forEach((reference, index) => {
    const summary = setResolvedSummary(reference);
    const dependency = setItemDependency(reference);
    const range = setGroupRangeAt(items, index);
    const groupLeader = range.start === index;
    const li = document.createElement("li");
    li.className = "set-item-card";
    if (dependency?.group_key) li.classList.add("set-shared-item");
    if (dependency?.group_key && groupLeader) li.classList.add("set-shared-first");
    if (dependency?.group_key && range.end === index) li.classList.add("set-shared-last");
    li.draggable = groupLeader;
    li.dataset.index = String(index);
    if (dependency?.group_key) li.dataset.groupKey = dependency.group_key;
    const number = document.createElement("span");
    number.className = "set-item-number";
    const copy = document.createElement("div");
    copy.className = "set-item-copy";
    copy.append(
      textNode("strong", summary ? `${bankItemNumber(summary)} · ${bankItemTags(summary).unit || "단원 미분류"}` : "사용할 수 없는 문항"),
      textNode("span", summary ? `${bankItemSource(summary)} · 원본 번호 ${bankItemNumber(summary)}` : `${reference.project_id} / ${reference.item_id}`)
    );
    if (dependency?.group_key) copy.append(textNode("span", `${dependency.group_label} 공통 지문·그림 세트 ${dependency.sub_index}/${dependency.group_size}`, "set-group-label"));
    if (bankDuplicateWarning(summary)) copy.append(textNode("span", "유사·중복 문항 후보", "duplicate-badge"));
    const controls = document.createElement("div");
    controls.className = "set-item-controls";
    if (groupLeader) {
      const drag = textNode("button", dependency?.group_key ? "세트 끌기" : "끌기", "drag-handle");
      drag.type = "button";
      drag.setAttribute("aria-label", dependency?.group_key ? `${dependency.group_label} 세트 순서 끌기` : `${index + 1}번 문항 순서 끌기`);
      controls.append(drag);
      for (const [label, target] of [["위로", range.start - 1], ["아래로", range.end + 1]]) {
        const button = textNode("button", label);
        button.type = "button";
        button.disabled = target < 0 || target >= items.length || state.setBusy;
        button.addEventListener("click", () => moveSetItem(index, target));
        controls.append(button);
      }
    }
    const remove = textNode("button", "제외");
    remove.type = "button";
    remove.addEventListener("click", () => removeSetItem(index));
    controls.append(remove);
    li.addEventListener("dragstart", () => { if (!groupLeader) return; state.draggedSetIndex = range.start; li.dataset.dragging = "true"; });
    li.addEventListener("dragend", () => { state.draggedSetIndex = null; li.dataset.dragging = "false"; });
    li.addEventListener("dragover", (event) => event.preventDefault());
    li.addEventListener("drop", (event) => {
      event.preventDefault();
      if (Number.isInteger(state.draggedSetIndex)) moveSetItem(state.draggedSetIndex, index);
    });
    li.append(number, copy, controls);
    elements.setItemList.append(li);
  });
  renderQuestionSetList();
  renderBankItems();
}

async function selectQuestionSet(setId) {
  if (!setId || state.setBusy) return;
  setBankBusy(true);
  try {
    const payload = await api(endpoint("question-sets", setId));
    state.activeSet = questionSetFrom(payload);
    state.activeSetId = questionSetId(state.activeSet);
    state.setDirty = false;
    renderActiveQuestionSet();
  } catch (error) {
    notify(error.message || "문제지를 불러오지 못했습니다.", "error");
  } finally {
    setBankBusy(false);
  }
}

async function createNewQuestionSet() {
  if (state.setBusy) return null;
  setBankBusy(true);
  try {
    const payload = await api(endpoint("question-sets"), {
      method: "POST",
      body: { title: "새 문제지", output_mode: "separate" }
    });
    const created = questionSetFrom(payload);
    state.activeSet = created;
    state.activeSetId = questionSetId(created);
    await loadQuestionSets(state.activeSetId);
    renderActiveQuestionSet();
    return created;
  } catch (error) {
    notify(error.message || "새 문제지를 만들지 못했습니다.", "error");
    return null;
  } finally {
    setBankBusy(false);
  }
}

async function saveActiveQuestionSet(items = questionSetItems(), announcement = "문제지를 저장했습니다.") {
  if (!state.activeSet || state.setBusy) return false;
  setBankBusy(true);
  try {
    const outputMode = document.querySelector('input[name="setOutputMode"]:checked')?.value || state.activeSet.output_mode || "separate";
    const payload = await api(endpoint("question-sets", state.activeSetId), {
      method: "PATCH",
      body: {
        expected_revision: Number(state.activeSet.revision),
        title: elements.setTitle.value.trim() || "새 문제지",
        output_mode: outputMode,
        items: items.map((value) => ({ project_id: String(value.project_id), item_id: String(value.item_id) }))
      }
    });
    state.activeSet = questionSetFrom(payload);
    state.activeSetId = questionSetId(state.activeSet);
    state.setDirty = false;
    const index = state.questionSets.findIndex((value) => questionSetId(value) === state.activeSetId);
    if (index >= 0) state.questionSets[index] = state.activeSet;
    else state.questionSets.push(state.activeSet);
    renderActiveQuestionSet();
    if (announcement) notify(announcement);
    return true;
  } catch (error) {
    notify(error.status === 409 ? "문제지가 다른 화면에서 변경되었습니다. 다시 불러와 주세요." : error.message || "문제지를 저장하지 못했습니다.", "error");
    return false;
  } finally {
    setBankBusy(false);
  }
}

async function addSelectedBankItems() {
  const targetId = elements.bankTargetSet.value;
  const selectedKeys = new Set(state.bankSelection);
  if (!targetId || !selectedKeys.size || state.setBusy) return;
  if (state.activeSetId !== targetId) await selectQuestionSet(targetId);
  if (state.activeSetId !== targetId || !state.activeSet) {
    elements.bankSelectionStatus.textContent = "대상 문제지를 불러오지 못했습니다. 선택은 유지됩니다.";
    return;
  }
  const existing = new Set(questionSetItems().map(bankRefKey));
  const plan = bankSelectionSavePlan(selectedKeys);
  if (!plan.items.length) {
    elements.bankSelectionStatus.textContent = "선택한 문항은 이미 대상 문제지에 있습니다.";
    return;
  }
  const attempted = new Set(plan.items.map(bankRefKey));
  const items = [...questionSetItems().map((value) => ({ ...value })), ...plan.items.map((item) => {
    const reference = item?.ref ?? item?.reference ?? item;
    return { project_id: String(reference.project_id), item_id: String(reference.item_id) };
  })];
  if (!await saveActiveQuestionSet(items, null)) {
    elements.bankSelectionStatus.textContent = "문제지를 저장하지 못했습니다. 선택은 유지됩니다.";
    return;
  }
  const saved = new Set(questionSetItems().map(bankRefKey));
  for (const key of selectedKeys) if (attempted.has(key) && saved.has(key)) state.bankSelection.delete(key);
  saveBankSelection();
  syncBankQuestionSelectionUi();
  const added = [...attempted].filter((key) => !existing.has(key) && saved.has(key)).length;
  elements.bankSelectionStatus.textContent = plan.extraCount || added !== selectedKeys.size
    ? `선택 ${selectedKeys.size}개 중 ${added}개 담았습니다.${plan.extraCount ? ` 묶음 ${plan.extraCount}개 포함.` : ""}` : "";
}

function selectedFigureRequestItems() {
  return selectedBankFigureItems().map((item) => {
    const source = item?.summary && typeof item.summary === "object" ? item.summary : item;
    const reference = source?.ref ?? source?.reference ?? source;
    const sourceRevision = Number(source?.revision ?? source?.workflow?.revision);
    if (!Number.isInteger(sourceRevision) || sourceRevision < 1) {
      throw new Error(`${bankItemNumber(item)} 문항의 현재 버전을 확인할 수 없습니다.`);
    }
    return {
      project_id: String(reference.project_id),
      item_id: String(reference.item_id),
      source_revision: sourceRevision
    };
  });
}

function blockedTargetCount(plan) {
  const counts = plan?.blocked_counts;
  const fromCounts = counts && typeof counts === "object"
    ? Object.values(counts).reduce((total, value) => total + Math.max(0, Number(value) || 0), 0)
    : 0;
  return Math.max(fromCounts, Array.isArray(plan?.blocked_targets) ? plan.blocked_targets.length : 0);
}

function figurePreflightConfirmation(plan) {
  const maximum = Number(plan.estimated_output_usd_max);
  const cost = Number.isFinite(maximum) ? `$${maximum.toFixed(3)}` : "확인 불가";
  const lines = [
    `선택 문항 ${Number(plan.selected_item_count) || 0}개`,
    `문제 그림 ${Number(plan.figure_count) || 0}개`,
    `재생성 대상 ${Number(plan.eligible_targets) || 0}개`,
    `기존 생성물·캐시 재사용 ${Number(plan.cached_targets) || 0}개`,
    `고유 생성 작업 ${Number(plan.unique_generation_keys) || 0}개`,
    `실제 최대 AI 호출 ${Number(plan.provider_calls_max) || 0}회`,
    `최대 예상 이미지 출력 비용 ${cost}`,
    `영역 분리 필요·보호 ${blockedTargetCount(plan)}개`
  ];
  if (plan.input_and_token_costs_included === false) {
    lines.push("입력·토큰 비용은 위 금액에 포함되지 않습니다.");
  }
  if (plan.pricing_as_of) lines.push(`비용 기준일 ${plan.pricing_as_of}`);
  return lines.join(" · ");
}

let pendingBankFigurePlan = null;

async function regenerateSelectedBankFigures() {
  if (state.bankBusy || !state.bankSelection.size) return;
  let items;
  try {
    items = selectedFigureRequestItems();
  } catch (error) {
    bankMoveStatus(error.message || "선택 문항을 확인하지 못했습니다.");
    return;
  }
  if (!items.length) {
    bankMoveStatus("재생성 가능한 문항이 없습니다.");
    return;
  }
  const excluded = state.bankSelection.size - items.length;
  const signature = JSON.stringify(items);
  if (pendingBankFigurePlan?.signature === signature) {
    const plan = pendingBankFigurePlan.plan;
    pendingBankFigurePlan = null;
    elements.regenerateSelectedFigures.textContent = "그림 재생성";
    bankMoveStatus("그림 재생성 작업을 등록하는 중입니다…");
    try {
      const job = await api(endpoint("bank", "figures", "batches"), {
        method: "POST", body: { plan_id: plan.plan_id, plan_sha256: plan.plan_sha256, confirmed: true }
      });
      const id = batchId(job);
      if (!id) throw new Error("그림 재생성 작업 정보를 받지 못했습니다.");
      state.batches = [job, ...state.batches.filter((entry) => batchId(entry) !== id)];
      state.batchStatuses.set(id, batchState(job));
      renderBatches();
      bankMoveStatus("그림 재생성 작업을 시작했습니다.");
    } catch (error) {
      bankMoveStatus(error.message || "그림 재생성 작업을 시작하지 못했습니다.");
    }
    return;
  }
  pendingBankFigurePlan = null;
  elements.regenerateSelectedFigures.textContent = "그림 재생성";
  bankMoveStatus("그림 재생성 비용 확인 중…");
  try {
    const plan = await api(endpoint("bank", "figures", "preflight"), {
      method: "POST",
      body: { items }
    });
    if (!plan?.plan_id || !plan?.plan_sha256) throw new Error("그림 재생성 사전 확인 결과가 올바르지 않습니다.");
    if (!Number(plan.eligible_targets)) {
      bankMoveStatus("재생성 대상이 없습니다.");
      return;
    }
    pendingBankFigurePlan = { signature, selectionKeys: [...state.bankSelection].sort().join("|"), plan };
    elements.regenerateSelectedFigures.textContent = "비용 확인 후 실행";
    bankMoveStatus(`${figurePreflightConfirmation(plan)}${excluded ? ` · 제외 ${excluded}개` : ""}`);
  } catch (error) {
    bankMoveStatus(error.message || "그림 재생성 비용을 확인하지 못했습니다.");
  }
}

async function moveSetItem(fromIndex, toIndex) {
  const items = questionSetItems().map((value) => ({ ...value }));
  if (fromIndex < 0 || toIndex < 0 || fromIndex >= items.length || toIndex >= items.length) return;
  const sourceRange = setGroupRangeAt(items, fromIndex);
  const targetRange = setGroupRangeAt(items, toIndex);
  if (sourceRange.start === targetRange.start && sourceRange.end === targetRange.end) return;
  const moved = items.splice(sourceRange.start, sourceRange.end - sourceRange.start + 1);
  const insertAt = targetRange.start < sourceRange.start
    ? targetRange.start
    : targetRange.end + 1 - moved.length;
  items.splice(insertAt, 0, ...moved);
  const label = sourceRange.key ? `${sourceRange.label} 세트` : `${sourceRange.start + 1}번 문항`;
  const message = `${label}가 ${insertAt + 1}번 위치로 이동했습니다.`;
  if (await saveActiveQuestionSet(items, "")) {
    elements.setOrderAnnouncement.textContent = message;
    const focusIndex = Math.min(insertAt, elements.setItemList.children.length - 1);
    elements.setItemList.children[focusIndex]?.querySelector(".set-item-controls button")?.focus();
  }
}

async function removeSetItem(index) {
  const items = questionSetItems().map((value) => ({ ...value }));
  if (index < 0 || index >= items.length) return;
  items.splice(index, 1);
  if (await saveActiveQuestionSet(items, "문항을 문제지에서 제외했습니다.")) {
    elements.setOrderAnnouncement.textContent = `${index + 1}번 문항을 제외했습니다.`;
  }
}

function renderQuestionSetBuild(payload) {
  elements.setBuildResult.hidden = false;
  elements.setBuildResult.dataset.state = "success";
  elements.setBuildStatus.textContent = state.activeSet?.output_mode === "combined_end_section"
    ? "문제와 답·풀이 미주를 한 파일로 만들었습니다."
    : "문제와 해설 HWPX를 따로 만들었습니다.";
  clearChildren(elements.setBuildChecks);
  clearChildren(elements.setDownloadLinks);
  const outputs = hwpxOutputList(payload);
  outputs.forEach((output) => {
    const kind = String(output?.document_kind ?? output?.artifact_kind ?? output?.kind ?? "question");
    const label = kind === "combined" ? "문제+미주 해설" : hwpxKindLabel(kind);
    if (typeof output?.download_url !== "string" || !output.download_url.startsWith("/api/")) return;
    const link = textNode("a", `${label} HWPX 받기`, "button");
    link.href = output.download_url;
    link.download = String(output.name || `${state.activeSet?.title || "문제지"}-${label}.hwpx`);
    elements.setDownloadLinks.append(link);
  });
  if (!elements.setDownloadLinks.children.length) throw new Error("문제지 다운로드 정보를 받지 못했습니다.");
}

async function buildActiveQuestionSetHwpx() {
  if (!state.activeSet || state.setBusy || !questionSetItems().length) return;
  setBankBusy(true);
  elements.setBuildResult.hidden = false;
  elements.setBuildStatus.textContent = "HWPX를 만드는 중입니다…";
  clearChildren(elements.setBuildChecks);
  clearChildren(elements.setDownloadLinks);
  try {
    const payload = await api(endpoint("question-sets", state.activeSetId, "hwpx", "build"), { method: "POST", body: {} });
    renderQuestionSetBuild(payload);
  } catch (error) {
    elements.setBuildResult.dataset.state = "error";
    elements.setBuildStatus.textContent = error.message || "문제지 HWPX를 만들지 못했습니다.";
    notify(elements.setBuildStatus.textContent, "error");
  } finally {
    setBankBusy(false);
  }
}

async function validateItem() {
  if (state.busy || !state.package) return;
  const packageData = parseEditor();
  if (!packageData) {
    elements.editor.focus();
    return;
  }
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", state.projectId, "items", state.itemId, "validate"), {
      method: "POST",
      body: { expected_revision: state.revision, package: packageData }
    });
    state.issues = issuesFromValidation(payload);
    renderIssues();
    const hasErrors = state.issues.some((issue) => issue.severity === "error");
    notify(
      state.issues.length
        ? `확인 항목 ${state.issues.length}개가 있습니다. 저장된 문항은 계속 사용할 수 있습니다.`
        : "검증을 통과했습니다.",
      hasErrors ? "error" : "info"
    );
  } catch (error) {
    const responseIssues = issuesFromValidation(error.body);
    state.issues = responseIssues.length ? responseIssues : normalizeIssues(error.message);
    renderIssues();
    notify("검증 요청을 완료하지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

async function importProject(event) {
  event.preventDefault();
  const sourcePath = elements.importPath.value.trim();
  if (!sourcePath || state.busy) return;
  if (!confirmDiscard()) return;
  setBusy(true);
  try {
    const payload = await api(endpoint("projects", "import"), { method: "POST", body: { source_path: sourcePath } });
    const id = payload?.project_id ?? payload?.project?.project_id ?? payload?.data?.project_id ?? null;
    elements.importPath.value = "";
    notify("원본을 변경하지 않고 새 workspace로 가져왔습니다.");
    await loadProjects(id);
  } catch (error) {
    notify(error.message || "프로젝트를 가져오지 못했습니다.", "error");
  } finally {
    setBusy(false);
  }
}

elements.batchForm.addEventListener("submit", startBatch);
elements.refreshBatches.addEventListener("click", () => loadBatches());
elements.refreshLearning.addEventListener("click", loadLearningLibrary);
elements.importForm.addEventListener("submit", importProject);
elements.refreshProjects.addEventListener("click", () => { if (confirmDiscard()) loadProjects(); });
elements.refreshTrash.addEventListener("click", loadTrash);
elements.ocrMissing.addEventListener("click", transcribeMissingQuestions);
elements.relinkSolutions.addEventListener("click", relinkProjectSolutions);
elements.tagAllItems.addEventListener("click", autoTagAllItems);
elements.buildHwpx.addEventListener("click", buildProjectHwpx);
elements.archiveProject.addEventListener("click", exportProjectArchive);
elements.itemSearch.addEventListener("input", renderItems);
elements.statusFilter.addEventListener("change", renderItems);
elements.tagFilter.addEventListener("change", renderItems);
elements.figureStatusFilter.addEventListener("change", renderItems);
elements.reload.addEventListener("click", () => { if (confirmDiscard()) loadItem(); });
elements.solutionPropagationToggle.addEventListener("change", () => {
  invalidateSolutionPropagationPreview();
  renderSolutionPropagationControls();
});
elements.previewSolutionPropagation.addEventListener("click", previewSolutionPropagation);
elements.applySolutionPropagation.addEventListener("click", applySolutionPropagation);
elements.solutionFeedbackAttested.addEventListener("change", renderSolutionFeedbackControls);
elements.solutionFeedbackPart.addEventListener("change", renderSolutionFeedbackControls);
elements.solutionFeedbackCorrect.addEventListener("click", () => applySolutionFeedback("correct_pair"));
elements.solutionFeedbackReject.addEventListener("click", () => applySolutionFeedback("rejected_pair"));
elements.solutionFeedbackCrop.addEventListener("click", () => applySolutionFeedback("crop_bounds"));
elements.solutionFeedbackSaveHeading.addEventListener("click", () => applySolutionFeedback("heading_role"));
elements.solutionFeedbackAlias.addEventListener("click", () => applySolutionFeedback("source_number_alias"));
elements.transcribe.addEventListener("click", transcribeItem);
elements.save.addEventListener("click", saveItem);
elements.tagForm.addEventListener("submit", saveItemTags);
elements.autoTagItem.addEventListener("click", autoTagCurrentItem);
const bankClassificationQuery = window.matchMedia("(max-width: 900px)");
let bankClassificationExpanded = !bankClassificationQuery.matches;
let bankClassificationWasNarrow = bankClassificationQuery.matches;
function syncBankClassification() {
  const isNarrow = bankClassificationQuery.matches;
  if (isNarrow !== bankClassificationWasNarrow) bankClassificationExpanded = !isNarrow;
  bankClassificationWasNarrow = isNarrow;
  elements.bankClassificationToggle.hidden = !isNarrow;
  elements.bankClassificationToggle.setAttribute("aria-expanded", String(!isNarrow || bankClassificationExpanded));
  elements.bankClassificationToggle.textContent = bankClassificationExpanded ? "분류 필터 접기" : "분류 필터 펼치기";
  elements.bankClassification.hidden = isNarrow && !bankClassificationExpanded;
}
elements.bankClassificationToggle.addEventListener("click", () => {
  bankClassificationExpanded = !bankClassificationExpanded;
  syncBankClassification();
});
bankClassificationQuery.addEventListener("change", syncBankClassification);
syncBankClassification();
elements.refreshBank.addEventListener("click", loadBankItems);
[elements.bankSearch, elements.bankNumberSearch].forEach((control) => control.addEventListener("input", readBankSearchControls));
Object.values(BANK_FILTERS).forEach((control) => control.addEventListener("change", readBankSearchControls));
elements.bankSort.addEventListener("change", () => changeBankSearch({ sort: elements.bankSort.value }));
elements.bankPageSize.addEventListener("change", () => changeBankSearch({ pageSize: Number(elements.bankPageSize.value) }));
elements.bankPreviousPage.addEventListener("click", () => changeBankSearch({ page: Math.max(1, activeBankSearch(state.bankSearch).page - 1) }, false));
elements.bankNextPage.addEventListener("click", () => changeBankSearch({ page: activeBankSearch(state.bankSearch).page + 1 }, false));
elements.newBankSearch.addEventListener("click", () => {
  updateBankSearch(state.bankSearch, { scrollY: window.scrollY });
  const tab = newBankSearch(state.bankSearch);
  syncBankSearchControls();
  renderBankItems();
  saveBankSearch();
  window.scrollTo(0, tab.scrollY);
});
elements.resetBankSearch.addEventListener("click", () => {
  changeBankSearch({ query: "", numberQuery: "", filters: {}, sort: "original", page: 1, pageSize: 20, scrollY: 0 });
  syncBankSearchControls();
  window.scrollTo(0, 0);
});
elements.clearBankSelection.addEventListener("click", () => {
  state.bankSelection.clear();
  saveBankSelection();
  syncBankQuestionSelectionUi();
});
elements.selectBankPage.addEventListener("click", () => selectBankPageItems());
document.querySelectorAll("[data-bank-quick-select]").forEach((button) => {
  button.addEventListener("click", () => selectBankPageItems(Number(button.dataset.bankQuickSelect)));
});
elements.addSelectedBankItems.addEventListener("click", addSelectedBankItems);
elements.regenerateSelectedFigures.addEventListener("click", regenerateSelectedBankFigures);
elements.deleteSelectedBankItems.addEventListener("click", moveSelectedBankItems);
elements.bankTargetSet.addEventListener("change", async () => {
  const id = elements.bankTargetSet.value;
  if (id) await selectQuestionSet(id);
  syncBankQuestionSelectionUi();
});
elements.createSetFromBank.addEventListener("click", createNewQuestionSet);
elements.createQuestionSet.addEventListener("click", createNewQuestionSet);
elements.saveQuestionSet.addEventListener("click", () => saveActiveQuestionSet());
elements.buildQuestionSetHwpx.addEventListener("click", buildActiveQuestionSetHwpx);
elements.createDocument.addEventListener("click", async () => {
  if (!state.activeSet || state.setBusy || state.setDirty) {
    document.getElementById("documentEntryStatus").textContent = "문제지 변경을 먼저 저장해 주세요.";
    return;
  }
  elements.createDocument.disabled = true;
  try {
    const { set_id, expected_revision } = documentCreationPayload(state.activeSet);
    const result = await api("/api/documents", { method: "POST", body: { set_id, expected_revision } });
    location.assign(`/document.html?id=${encodeURIComponent(result.document.document_id)}`);
  } catch (error) {
    document.getElementById("documentEntryStatus").textContent = error.message;
    renderActiveQuestionSet();
  }
});
elements.setTitle.addEventListener("input", () => { state.setDirty = true; elements.createDocument.disabled = true; });
elements.setTitle.addEventListener("change", () => saveActiveQuestionSet());
document.querySelectorAll('input[name="setOutputMode"]').forEach((input) => {
  input.addEventListener("change", () => saveActiveQuestionSet());
});
elements.tagSubject.addEventListener("change", () => renderTagUnitOptions(elements.tagSubject.value, elements.tagUnit.value));
elements.validate.addEventListener("click", validateItem);
elements.approve.addEventListener("click", approveAndNext);
elements.nextReview.addEventListener("click", goToNextReviewItem);
elements.approveNext.addEventListener("click", approveAndNext);
elements.applyCandidate.addEventListener("click", applyCandidate);
elements.format.addEventListener("click", () => {
  if (requireFinishedRecrop("기술 정보 형식을 정리")) return;
  const parsed = parseEditor();
  if (!parsed) return;
  elements.editor.value = JSON.stringify(parsed, null, 2);
  setDirty(true);
  renderPreview(parsed);
});
elements.contentViewTab.addEventListener("click", () => setEditorView("content"));
elements.jsonViewTab.addEventListener("click", () => setEditorView("json"));
elements.editor.addEventListener("input", () => { setDirty(true); schedulePreview(); });
elements.overlayToggle.addEventListener("change", () => {
  state.overlaysVisible = elements.overlayToggle.checked;
  renderSourceOverlays();
});
elements.sourceImage.addEventListener("load", () => renderSourceOverlays());
elements.sourceImage.addEventListener("error", () => {
  elements.sourceCanvas.hidden = true;
  clearChildren(elements.sourceOverlayLayer);
  elements.sourcePlaceholder.hidden = false;
  elements.sourcePlaceholder.querySelector("strong").textContent = "이미지를 불러오지 못했습니다.";
});
elements.sourceAssetSelect.addEventListener("change", () => {
  if (state.figureRecropDraft && elements.sourceAssetSelect.value !== state.figureRecropDraft.sourceAssetId) {
    if (!window.confirm("수정 중인 그림 영역을 버리고 원본 이미지를 변경할까요?")) {
      elements.sourceAssetSelect.value = state.figureRecropDraft.sourceAssetId;
      return;
    }
    setFigureRecropDraft(null);
    elements.bboxEditStatus.textContent = "원본 이미지를 변경해 그림 영역 수정을 취소했습니다.";
  }
  state.assetIdBySection[state.section] = elements.sourceAssetSelect.value;
  renderSolutionLinkDetails();
  invalidateSolutionPropagationPreview("해설 원본이 바뀌었습니다. 영향을 다시 미리 보세요.");
  renderSource();
});
for (const [section, panel] of Object.entries(elements.readingPanels)) {
  if (!panel) continue;
  panel.querySelectorAll("[data-view-mode]").forEach((button) => button.addEventListener("click", () => {
    state.dualView[section].mode = button.dataset.viewMode;
    renderDualReading();
  }));
  panel.querySelector("[data-reading-asset]").addEventListener("change", (event) => {
    state.dualView[section].assetId = event.target.value;
    renderDualReading();
  });
  panel.querySelector(".dual-reading-body").addEventListener("scroll", (event) => {
    state.dualView[section].scrollTop = event.target.scrollTop;
  });
  panel.querySelector("[data-edit-section]").addEventListener("click", () => openEditorFor(section));
}
elements.readingPanels.explanation?.querySelector("[data-open-solution-tools]")?.addEventListener("click", () => {
  const tools = $("solutionImageTools");
  tools.open = true;
  tools.scrollIntoView({ behavior: "smooth", block: "nearest" });
});
elements.tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => setSection(tab.dataset.section));
  tab.addEventListener("keydown", (event) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % elements.tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + elements.tabs.length) % elements.tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = elements.tabs.length - 1;
    else return;
    event.preventDefault();
    setSection(elements.tabs[next].dataset.section, true);
  });
});

document.addEventListener("keydown", (event) => {
  if ((state.view === "saved" && new URLSearchParams(window.location.search).get("mode") === "read") || $("bankReadDialog").open) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    saveItem();
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedWork()) return;
  event.preventDefault();
  event.returnValue = "";
});

window.addEventListener("pagehide", () => {
  window.clearTimeout(state.batchPollTimer);
  window.clearTimeout(state.recoveryPollTimer);
  if (state.view === "bank") {
    updateBankSearch(state.bankSearch, { scrollY: window.scrollY });
    saveBankSearch();
    saveBankGroupExpansion();
  }
});

function openBankReadDialog(project, item) {
  const dialog = $("bankReadDialog");
  dialog.append($("bankReadOnlyView"));
  dialog.showModal();
  window.history.pushState({ bankReadDialog: true }, "", window.location.href);
  loadBankReadOnly(project, item);
}

$("closeBankReadDialog").addEventListener("click", () => $("bankReadDialog").close());
$("bankReadDialog").addEventListener("close", () => {
  bankReadRequest += 1;
  const view = $("bankReadOnlyView");
  view.hidden = true;
  $("mainContent").prepend(view);
  if (window.history.state?.bankReadDialog) window.history.back();
});
window.addEventListener("popstate", () => { if ($("bankReadDialog").open) $("bankReadDialog").close(); });

async function loadBankReadOnly(project, item) {
  const request = ++bankReadRequest;
  const view = $("bankReadOnlyView");
  const status = $("bankReadOnlyStatus");
  view.hidden = false;
  if (!project || !item) {
    status.textContent = "문항 주소가 올바르지 않습니다.";
    return;
  }
  status.textContent = "문항을 불러오는 중입니다…";
  try {
    const detail = detailFrom(await api(endpoint("projects", project, "items", item)));
    if (request !== bankReadRequest) return;
    if (detail.package?.item_id !== item) throw new Error("요청한 문항과 저장된 문항이 일치하지 않습니다.");
    setConnection(true, "연결됨");
    state.assetUrls = detail.assetUrls && typeof detail.assetUrls === "object" ? detail.assetUrls : {};
    $("bankReadOnlyTitle").textContent = `${detail.package?.identity?.printed_number || "문항"} 읽기`;
    for (const section of ["question", "explanation"]) {
      const body = $(section === "question" ? "bankReadQuestion" : "bankReadExplanation");
      const blocks = detail.package?.content?.[section]?.blocks;
      body.replaceChildren();
      if (Array.isArray(blocks) && blocks.length) {
        const reading = document.createElement("div");
        reading.className = "reading-preview";
        blocks.forEach((block) => reading.append(renderReadableBlock(block, detail.package)));
        body.append(reading);
      } else body.append(textNode("p", `타이핑된 ${section === "question" ? "문제" : "해설"} 없음`, "empty-note"));
    }
    status.textContent = "";
  } catch (error) {
    if (request !== bankReadRequest) return;
    setConnection(false, "연결할 수 없습니다");
    status.textContent = error.status === 404 || error.status === 410 ? "문항을 찾을 수 없습니다." : (error.message || "문항을 불러오지 못했습니다.");
  }
}

async function initialize() {
  applyWorkspaceView();
  const initialProject = new URLSearchParams(window.location.search).get("project");
  if (state.view === "saved" && new URLSearchParams(window.location.search).get("mode") === "read") {
    elements.savedView.dataset.mode = "read";
    elements.welcome.hidden = true;
    elements.workspace.hidden = true;
    await loadBankReadOnly(initialProject, new URLSearchParams(window.location.search).get("item"));
    return;
  }
  if (state.view === "saved") {
    await Promise.all([loadProjects(initialProject), loadRecoveryProgress()]);
    await loadTrash();
    state.recoveryPollTimer = window.setTimeout(pollRecoveryProgress, 5000);
  } else if (state.view === "bank") {
    restoreBankGroupExpansion();
    try {
      const pending = JSON.parse(window.sessionStorage.getItem(BANK_MOVE_KEY) || "null");
      if (pending && Array.isArray(pending.keys) && typeof pending.operation_id === "string") {
        pending.keys.forEach((key) => state.bankPending.add(key));
        recoverBankMove(pending);
      }
    } catch {}
    try {
      const config = await api(endpoint("config"));
      state.figureProfiles = config?.figure_profiles && typeof config.figure_profiles === "object" ? config.figure_profiles : {};
      setConnection(true, "연결됨");
    }
    catch { setConnection(false, "연결할 수 없습니다"); }
    await loadQuestionSets();
    await loadBankItems();
    renderBankItems();
    window.requestAnimationFrame(() => window.scrollTo(0, activeBankSearch(state.bankSearch).scrollY));
  } else if (state.view === "sets") {
    try { await api(endpoint("config")); setConnection(true, "연결됨"); }
    catch { setConnection(false, "연결할 수 없습니다"); }
    await loadQuestionSets(new URLSearchParams(window.location.search).get("set"));
    await loadBankItems();
    renderActiveQuestionSet();
  } else if (state.view === "learning") {
    await loadLearningLibrary();
  } else {
    await loadProjects(initialProject);
    await loadTrash();
  }
  if (state.view === "work" || state.view === "saved") {
    await loadBatches(true);
    state.batchPollTimer = window.setTimeout(pollBatches, state.view === "saved" ? 5000 : 2000);
  }
}

initialize();

// Visual solution crop keeps each page's rectangle until the user applies it.
let solutionCropDraft = null;
const cropUi = Object.fromEntries(["beginSolutionCrop", "solutionCropEditor", "solutionCropPage", "solutionCropZoom", "solutionCropZoomValue", "solutionCropCanvas", "solutionCropOriginal", "solutionCropBox", "solutionCropBefore", "solutionCropAfter", "applySolutionCrop", "cancelSolutionCrop", "solutionCropStatus"].map((id) => [id, $(id)]));
function cropPage() { return solutionCropDraft?.pages[Number(cropUi.solutionCropPage.value)] || null; }
function cropPoint(event) {
  const rect = cropUi.solutionCropCanvas.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) };
}
function drawSolutionCrop() {
  const page = cropPage();
  if (!page) return;
  const box = page.bbox;
  const overlay = cropUi.solutionCropBox;
  overlay.style.left = `${box.left * 100}%`; overlay.style.top = `${box.top * 100}%`;
  overlay.style.width = `${(box.right - box.left) * 100}%`; overlay.style.height = `${(box.bottom - box.top) * 100}%`;
  overlay.setAttribute("aria-valuenow", String(Math.round(box.left * 100)));
  const canvas = cropUi.solutionCropAfter;
  const image = cropUi.solutionCropOriginal;
  if (image.complete && image.naturalWidth) {
    canvas.width = Math.max(1, Math.round((box.right - box.left) * image.naturalWidth));
    canvas.height = Math.max(1, Math.round((box.bottom - box.top) * image.naturalHeight));
    canvas.getContext("2d").drawImage(image, box.left * image.naturalWidth, box.top * image.naturalHeight,
      canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
  }
}
function showSolutionCropPage() {
  const page = cropPage();
  if (!page) return;
  cropUi.solutionCropOriginal.src = page.asset_url;
  cropUi.solutionCropOriginal.onload = drawSolutionCrop;
  cropUi.solutionCropBefore.src = page.before_url || page.asset_url;
  drawSolutionCrop();
}
async function beginSolutionCrop() {
  if (!state.itemId || state.dirty) { cropUi.solutionCropStatus.textContent = "편집 중인 내용을 먼저 저장해 주세요."; return; }
  try {
    const selection = state.package?.provenance?.solution_selection;
    if (!selection) throw new Error("해설 원본을 먼저 선택해 주세요.");
    const result = await api(`${endpoint("projects", state.projectId, "items", state.itemId)}/solution-source/pages`);
    if (!result.pages?.length) throw new Error("선택한 해설의 원본 페이지를 열 수 없습니다.");
    const links = state.package.provenance.solution_links || [];
    const link = links.find((value) => value?.source_asset_id === selection.source_asset_id);
    const asset = state.package.resources.assets.find((value) => value?.asset_id === selection.source_asset_id);
    const beforeUrl = asset && apiAssetUrl(asset);
    const pages = result.pages.map((page) => {
      const part = link?.crop_parts?.find((value) => value.page_ref === page.page_ref) || link?.parts?.find((value) => value.pdf_page === page.pdf_page);
      const partOrder = link?.parts?.findIndex((value) => value.pdf_page === page.pdf_page) ?? -1;
      return { ...page, order: partOrder >= 0 ? partOrder : 1000 + (page.pdf_page || 0), included: Boolean(part) || !link?.parts?.length, bbox: part?.bbox ? { ...part.bbox } : { left: 0, top: 0, right: 1, bottom: 1 }, before_url: beforeUrl };
    });
    solutionCropDraft = { base: selection.source_asset_id, revision: selection.revision, itemRevision: state.revision, pages };
    cropUi.solutionCropPage.replaceChildren(...pages.map((page, index) => {
      const option = document.createElement("option"); option.value = String(index);
      option.textContent = page.pdf_page ? `PDF ${page.pdf_page}쪽` : `이미지 ${index + 1}`; return option;
    }));
    cropUi.solutionCropEditor.hidden = false;
    cropUi.solutionCropStatus.textContent = "";
    showSolutionCropPage();
  } catch (error) { cropUi.solutionCropStatus.textContent = error.message || "원본 페이지를 열지 못했습니다."; }
}
let cropDrag = null;
cropUi.solutionCropCanvas.addEventListener("pointerdown", (event) => {
  if (!solutionCropDraft) return;
  const point = cropPoint(event), box = cropPage().bbox;
  const corner = event.target.classList?.[0];
  cropDrag = { start: point, original: { ...box }, mode: ["nw", "ne", "sw", "se"].includes(corner) ? corner : event.target === cropUi.solutionCropBox ? "move" : "new" };
  cropUi.solutionCropCanvas.setPointerCapture(event.pointerId);
  event.preventDefault();
});
cropUi.solutionCropCanvas.addEventListener("pointermove", (event) => {
  if (!cropDrag || !cropPage()) return;
  const point = cropPoint(event), start = cropDrag.start, old = cropDrag.original, dx = point.x - start.x, dy = point.y - start.y;
  let next;
  if (cropDrag.mode === "new") next = { left: Math.min(start.x, point.x), top: Math.min(start.y, point.y), right: Math.max(start.x, point.x), bottom: Math.max(start.y, point.y) };
  else if (cropDrag.mode === "move") { const x = Math.max(-old.left, Math.min(dx, 1 - old.right)), y = Math.max(-old.top, Math.min(dy, 1 - old.bottom)); next = { left: old.left + x, right: old.right + x, top: old.top + y, bottom: old.bottom + y }; }
  else { next = { ...old }; for (const side of ["left", "right", "top", "bottom"]) if (({ left: "nw sw", right: "ne se", top: "nw ne", bottom: "sw se" })[side].includes(cropDrag.mode)) next[side] = Math.max(0, Math.min(1, old[side] + (side === "left" || side === "right" ? dx : dy))); }
  if (next.right - next.left >= BBOX_MIN_SIZE && next.bottom - next.top >= BBOX_MIN_SIZE) { cropPage().bbox = next; cropPage().included = true; drawSolutionCrop(); }
});
cropUi.solutionCropCanvas.addEventListener("pointerup", () => { cropDrag = null; cropUi.solutionCropBox.focus(); });
cropUi.solutionCropBox.addEventListener("keydown", (event) => {
  if (!solutionCropDraft || !event.key.startsWith("Arrow")) return;
  event.preventDefault(); cropPage().bbox = adjustedBox(cropPage().bbox, event.key, event.shiftKey, event.ctrlKey ? BBOX_FINE_STEP : BBOX_STEP); cropPage().included = true; drawSolutionCrop();
});
cropUi.solutionCropPage.addEventListener("change", showSolutionCropPage);
cropUi.solutionCropZoom.addEventListener("input", () => { const zoom = Number(cropUi.solutionCropZoom.value); cropUi.solutionCropCanvas.style.width = `${zoom}%`; cropUi.solutionCropZoomValue.textContent = `${zoom}%`; });
cropUi.beginSolutionCrop.addEventListener("click", beginSolutionCrop);
cropUi.cancelSolutionCrop.addEventListener("click", () => { solutionCropDraft = null; cropUi.solutionCropEditor.hidden = true; cropUi.solutionCropStatus.textContent = ""; });
cropUi.applySolutionCrop.addEventListener("click", async () => {
  const draft = solutionCropDraft;
  if (!draft || state.dirty) return;
  cropUi.applySolutionCrop.disabled = true;
  try {
    const result = await api(`${endpoint("projects", state.projectId, "items", state.itemId)}/solution-source`, { method: "POST", body: {
      operation_id: `solution-${crypto.randomUUID()}`, expected_revision: draft.itemRevision, expected_selection_revision: draft.revision,
      source: { kind: "crop", base_source_asset_id: draft.base, parts: draft.pages.filter((page) => page.included).sort((a, b) => a.order - b.order).map((page) => ({ page_ref: page.page_ref, bbox: { unit: "ratio", origin: "top-left", ...page.bbox } })) }
    }});
    solutionCropDraft = null; cropUi.solutionCropEditor.hidden = true;
    elements.solutionImageStatus.textContent = `수정한 해설 이미지로 타이핑 작업 ${result.job_id}을 진행합니다.`;
    await loadItem();
  } catch (error) { cropUi.solutionCropStatus.textContent = error.message || "영역을 적용하지 못했습니다."; }
  finally { cropUi.applySolutionCrop.disabled = false; }
});
