<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
} from "vue";

import {
  ProfileExtractionClient,
  newChannelId,
  newChecklistItemId,
  type ChannelAttributeSuggestion,
  type ChecklistPriority,
  type CollectionDocument,
  type ComparisonDocumentResult,
  type ComparisonOutcome,
  type DocumentChecklist,
  type DocumentChecklistItem,
  type DocumentDetailBlock,
  type ExtractionChannel,
  type ExtractionProfile,
  type ExtractionProfileConfig,
  type PreviewCandidate,
  type PreviewRunDetail,
  type PreviewRunPlan,
  type PreviewRunStatus,
  type PreviewRunSummary,
  type ProfileComparison,
  type ProfileSelection,
} from "./profile-extraction";
import FieldHelp from "./FieldHelp.vue";

const props = defineProps<{
  baseUrl: string;
  token: string;
}>();

/** Profile 工作区四个稳定子页面。 */
type WorkspaceTab = "profiles" | "preview" | "checklists" | "comparisons";

/** 候选卡片展示的原文证据块。 */
type PreviewEvidenceBlock = PreviewCandidate["evidence_blocks"][number];

/** 可送入证据翻译缓存的最小原文块契约。 */
type TranslatableSourceBlock = Pick<DocumentDetailBlock, "block_id" | "text">;

/** 应用内不可逆或高成本操作的用户确认请求。 */
interface ConfirmationRequest {
  title: string;
  message: string;
  details?: string[];
  confirmLabel: string;
  isDangerous?: boolean;
  onConfirm: () => Promise<void>;
}

const client = new ProfileExtractionClient(props.baseUrl, props.token);
const activeTab = ref<WorkspaceTab>("profiles");
const busy = ref(false);
const error = ref("");
const message = ref("");
const profiles = ref<ExtractionProfile[]>([]);
const selection = ref<ProfileSelection>({
  selected_profile_id: null,
  selected_profile_hash: null,
  selected_at: null,
});
const editingProfile = ref<ExtractionProfile | null>(null);
const editingConfig = ref<ExtractionProfileConfig | null>(null);
const editingReadOnly = ref(false);
const pendingSelectionProfile = ref<ExtractionProfile | null>(null);
const showSelectionDialog = ref(false);
const pendingConfirmation = ref<ConfirmationRequest | null>(null);
const confirmationBusy = ref(false);
const validationMessages = ref<string[]>([]);
const compiledPrompts = ref<Array<Record<string, string>>>([]);
const showCompiledPrompts = ref(false);
const strategySuggestion = ref<{
  channels: ExtractionChannel[];
  exclude_criteria: string[];
} | null>(null);
const channelAttributeSuggestions = reactive<Record<string, ChannelAttributeSuggestion>>({});
const generatingChannelId = ref("");

const documents = ref<CollectionDocument[]>([]);
const snapshotId = ref("");
const previewProfileId = ref("");
const previewDocumentIds = ref<string[]>([]);
const previewPlan = ref<PreviewRunPlan | null>(null);
const forceNewPreview = ref(false);
const previewRuns = ref<PreviewRunSummary[]>([]);
const showPreviewHistory = ref(true);
const previewHistoryQuery = ref("");
const previewHistoryStatus = ref<PreviewRunStatus | "">("");
const activePreview = ref<PreviewRunDetail | null>(null);
const activePreviewCandidates = ref<PreviewCandidate[]>([]);
const selectedPreviewCandidateId = ref("");
const previewCandidateQuery = ref("");
const previewCandidateDocId = ref("");
const previewCandidateChannelId = ref("");
const previewResultView = ref<"candidates" | "source-order">("candidates");
const sourceOrderDocId = ref("");
const sourceOrderBlocks = ref<DocumentDetailBlock[]>([]);
const sourceOrderBusy = ref(false);
const sourceOrderOnlyUncovered = ref(false);
const sourceOrderLanguage = ref<"source" | "zh">("source");
const sourceCandidateDetailId = ref("");
const activePreviewTranslations = reactive<Record<string, string>>({});
const evidenceTranslationBusy = ref(false);
const evidenceTranslationNotice = ref("");
const previewCandidatePriority = ref<ChecklistPriority>("should");
const pollHandle = ref<number | null>(null);

const checklists = ref<DocumentChecklist[]>([]);
const checklistDocId = ref("");
const checklistNote = ref("");
const activeChecklist = ref<DocumentChecklist | null>(null);
const checklistItems = ref<DocumentChecklistItem[]>([]);
const checklistBlocks = ref<DocumentDetailBlock[]>([]);
const manualItemName = ref("");
const manualItemPriority = ref<ChecklistPriority>("should");
const manualItemBlockId = ref("");

const comparisonRunAId = ref("");
const comparisonRunBId = ref("");
const selectedChecklistIds = ref<string[]>([]);
const activeComparison = ref<ProfileComparison | null>(null);
const comparisonRunA = ref<PreviewRunDetail | null>(null);
const comparisonRunB = ref<PreviewRunDetail | null>(null);
const comparisonOutcome = ref<ComparisonOutcome>("equivalent");
const comparisonNote = ref("");
const mustOverrideReasons = reactive<Record<string, string>>({});
const documentOutcomes = reactive<Record<string, ComparisonOutcome>>({});
const uncertainCandidateChoice = reactive<Record<string, string>>({});
const comparisons = ref<ProfileComparison[]>([]);

const selectedProfileName = computed(
  () =>
    profiles.value.find(
      (profile) => profile.profile_id === selection.value.selected_profile_id,
    )?.config.name ?? "尚未选择",
);
const selectedProfile = computed(
  () =>
    profiles.value.find(
      (profile) => profile.profile_id === selection.value.selected_profile_id,
    ) ?? null,
);
const completedRuns = computed(() =>
  previewRuns.value.filter((run) => run.status === "completed"),
);
const filteredPreviewRuns = computed(() => {
  const query = previewHistoryQuery.value.trim().toLocaleLowerCase();
  return previewRuns.value.filter((run) => {
    const matchesQuery =
      !query ||
      profileNameForRun(run).toLocaleLowerCase().includes(query) ||
      run.run_id.toLocaleLowerCase().includes(query);
    const matchesStatus =
      !previewHistoryStatus.value || run.status === previewHistoryStatus.value;
    return matchesQuery && matchesStatus;
  });
});
const previewCandidateDocuments = computed(() => {
  const documentIds = new Set(
    activePreviewCandidates.value.map((candidate) => candidate.doc_id),
  );
  return documents.value.filter((document) => documentIds.has(document.doc_id));
});
const activePreviewDocuments = computed(() => {
  const documentIds = new Set(activePreview.value?.document_scope ?? []);
  return documents.value.filter((document) => documentIds.has(document.doc_id));
});
const previewCandidateChannels = computed(() => {
  const channelIds = new Set(
    activePreviewCandidates.value.flatMap((candidate) => candidate.channel_ids),
  );
  return [...channelIds].map((channelId) => ({
    channelId,
    name: channelNameForId(channelId),
  }));
});
const filteredPreviewCandidates = computed(() => {
  const query = previewCandidateQuery.value.trim().toLocaleLowerCase();
  return activePreviewCandidates.value.filter((candidate) => {
    const searchText = [
      candidate.name,
      candidate.summary,
      ...candidate.aliases,
    ]
      .join(" ")
      .toLocaleLowerCase();
    return (
      (!query || searchText.includes(query)) &&
      (!previewCandidateDocId.value ||
        candidate.doc_id === previewCandidateDocId.value) &&
      (!previewCandidateChannelId.value ||
        candidate.channel_ids.includes(previewCandidateChannelId.value))
    );
  });
});
const filteredPreviewCandidateGroups = computed(() =>
  previewCandidateDocuments.value
    .map((document) => ({
      document,
      candidates: filteredPreviewCandidates.value.filter(
        (candidate) => candidate.doc_id === document.doc_id,
      ),
    }))
    .filter((group) => group.candidates.length > 0),
);
const selectedPreviewCandidate = computed(() => {
  return (
    filteredPreviewCandidates.value.find(
      (candidate) => candidate.candidate_id === selectedPreviewCandidateId.value,
    ) ??
    filteredPreviewCandidates.value[0] ??
    null
  );
});
const sourceOrderCandidateMap = computed(() => {
  const blocks = new Map<string, PreviewCandidate[]>();
  for (const candidate of activePreviewCandidates.value) {
    for (const blockId of candidate.source_block_ids) {
      const linked = blocks.get(blockId) ?? [];
      linked.push(candidate);
      blocks.set(blockId, linked);
    }
  }
  return blocks;
});
const sourceCandidateFirstBlockIds = computed(() => {
  const firstBlockByCandidate = new Map<string, string>();
  for (const block of sourceOrderBlocks.value) {
    for (const candidate of sourceOrderCandidateMap.value.get(block.block_id) ?? []) {
      if (!firstBlockByCandidate.has(candidate.candidate_id)) {
        firstBlockByCandidate.set(candidate.candidate_id, block.block_id);
      }
    }
  }
  return firstBlockByCandidate;
});
const sourceOrderCoveredBlockCount = computed(
  () =>
    sourceOrderBlocks.value.filter((block) =>
      sourceOrderCandidateMap.value.has(block.block_id),
    ).length,
);
const sourceOrderVisibleBlocks = computed(() =>
  sourceOrderOnlyUncovered.value
    ? sourceOrderBlocks.value.filter(
        (block) => !sourceOrderCandidateMap.value.has(block.block_id),
      )
    : sourceOrderBlocks.value,
);
const englishSourceOrderBlocks = computed(() =>
  sourceOrderBlocks.value.filter((block) => requiresChineseTranslation(block.text)),
);
const untranslatedSourceOrderBlocks = computed(() =>
  englishSourceOrderBlocks.value.filter(
    (block) => !activePreviewTranslations[block.block_id],
  ),
);
const sourceCandidateDetail = computed(
  () =>
    activePreviewCandidates.value.find(
      (candidate) => candidate.candidate_id === sourceCandidateDetailId.value,
    ) ?? null,
);
const englishPreviewEvidence = computed(() => {
  const blocks = new Map<string, PreviewEvidenceBlock>();
  for (const candidate of activePreviewCandidates.value) {
    for (const evidence of candidate.evidence_blocks) {
      if (requiresChineseTranslation(evidence.text)) {
        blocks.set(evidence.block_id, evidence);
      }
    }
  }
  return [...blocks.values()];
});
const untranslatedEnglishEvidence = computed(() =>
  englishPreviewEvidence.value.filter(
    (evidence) => !activePreviewTranslations[evidence.block_id],
  ),
);
const frozenChecklists = computed(() =>
  checklists.value.filter((checklist) => checklist.status === "frozen"),
);

/** 统一清理上一轮提示并显示当前操作反馈。 */
function setFeedback(nextMessage = "", nextError = ""): void {
  message.value = nextMessage;
  error.value = nextError;
}

/** 打开一个统一风格的应用内确认框。 */
function requestConfirmation(request: ConfirmationRequest): void {
  pendingConfirmation.value = request;
}

/** 在操作未执行时关闭确认框，避免异步过程中误关闭。 */
function closeConfirmation(): void {
  if (!confirmationBusy.value) {
    pendingConfirmation.value = null;
  }
}

/** 执行确认框绑定操作；成功后关闭确认框。 */
async function executeConfirmation(): Promise<void> {
  const request = pendingConfirmation.value;
  if (!request || confirmationBusy.value) {
    return;
  }
  confirmationBusy.value = true;
  try {
    await request.onConfirm();
    pendingConfirmation.value = null;
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    confirmationBusy.value = false;
  }
}

/** 清除所有尚未应用的通道属性建议，避免编辑器切换后串用旧结果。 */
function clearChannelAttributeSuggestions(): void {
  for (const channelId of Object.keys(channelAttributeSuggestions)) {
    delete channelAttributeSuggestions[channelId];
  }
  generatingChannelId.value = "";
}

/** 从当前 Profile 列表解析显示名称。 */
function profileName(profileId: string): string {
  return (
    profiles.value.find((profile) => profile.profile_id === profileId)?.config.name ??
    profileId
  );
}

/** 优先使用 Run 内冻结的 Profile 名称快照。 */
function profileNameForRun(run: PreviewRunSummary): string {
  return run.profile_name || profileName(run.profile_id);
}

/** 解析试抽取来源通道名称；历史配置缺失时回退到通道 ID。 */
function channelNameForId(channelId: string): string {
  const profile = profiles.value.find(
    (candidate) => candidate.profile_id === activePreview.value?.profile_id,
  );
  return (
    profile?.config.extraction_channels.find(
      (channel) => channel.channel_id === channelId,
    )?.name ?? channelId
  );
}

/** 选中候选详情，同时保持左侧列表的当前浏览位置。 */
function selectPreviewCandidate(candidateId: string): void {
  selectedPreviewCandidateId.value = candidateId;
}

/** 切换试抽取记录时清空候选筛选，避免旧条件隐藏新结果。 */
function resetPreviewCandidateFilters(): void {
  previewCandidateQuery.value = "";
  previewCandidateDocId.value = "";
  previewCandidateChannelId.value = "";
}

/** 切换试抽取记录时清空原文顺序视图，避免混用上一记录的文档 blocks。 */
function resetSourceOrderWorkspace(): void {
  previewResultView.value = "candidates";
  sourceOrderDocId.value = "";
  sourceOrderBlocks.value = [];
  sourceOrderOnlyUncovered.value = false;
  sourceOrderLanguage.value = "source";
  sourceCandidateDetailId.value = "";
}

/** 载入指定文档的完整原文 blocks，用于按原文顺序检查覆盖情况。 */
async function loadSourceOrderDocument(docId: string): Promise<void> {
  if (!activePreview.value || !docId) {
    sourceOrderDocId.value = "";
    sourceOrderBlocks.value = [];
    return;
  }
  const runId = activePreview.value.run_id;
  const snapshotId = activePreview.value.snapshot_id;
  sourceOrderDocId.value = docId;
  sourceOrderBusy.value = true;
  try {
    const blocks = await client.checklistBlocks(snapshotId, docId);
    if (activePreview.value?.run_id === runId) {
      sourceOrderBlocks.value = blocks;
      if (sourceOrderLanguage.value === "zh") {
        const untranslated = blocks.filter(
          (block) =>
            requiresChineseTranslation(block.text) &&
            !activePreviewTranslations[block.block_id],
        );
        if (untranslated.length) {
          void translateActiveEvidence(untranslated);
        }
      }
    }
  } catch (caught) {
    sourceOrderBlocks.value = [];
    setFeedback("", String(caught));
  } finally {
    sourceOrderBusy.value = false;
  }
}

/** 切到按原文阅读视图，并默认打开当前候选或当前结果的第一篇文档。 */
async function showSourceOrderView(): Promise<void> {
  previewResultView.value = "source-order";
  const currentDocumentIsAvailable = activePreviewDocuments.value.some(
    (document) => document.doc_id === sourceOrderDocId.value,
  );
  if (!currentDocumentIsAvailable) {
    const preferredDocId =
      selectedPreviewCandidate.value?.doc_id ??
      activePreviewDocuments.value[0]?.doc_id ??
      "";
    await loadSourceOrderDocument(preferredDocId);
  }
}

/** 返回候选主从浏览视图。 */
function showCandidateView(): void {
  previewResultView.value = "candidates";
}

/** 切换原文阅读语言；首次切到中文时补齐当前文档尚未缓存的英文块。 */
async function setSourceOrderLanguage(language: "source" | "zh"): Promise<void> {
  sourceOrderLanguage.value = language;
  if (language === "zh" && untranslatedSourceOrderBlocks.value.length) {
    await translateActiveEvidence(untranslatedSourceOrderBlocks.value);
  }
}

/** 返回引用某个原文 block 的全部候选。 */
function candidatesForSourceBlock(blockId: string): PreviewCandidate[] {
  return sourceOrderCandidateMap.value.get(blockId) ?? [];
}

/** 判断候选是否首次出现在当前原文块，后续关联使用紧凑展示。 */
function isFirstSourceCandidateOccurrence(candidateId: string, blockId: string): boolean {
  return sourceCandidateFirstBlockIds.value.get(candidateId) === blockId;
}

/** 为可见原文 blocks 计算分节标题，连续相同章节只显示一次。 */
function sourceOrderSectionLabel(index: number): string {
  const current = sourceOrderVisibleBlocks.value[index];
  const previous = sourceOrderVisibleBlocks.value[index - 1];
  if (!current) {
    return "";
  }
  if (
    previous &&
    previous.section_path.join("\u0000") === current.section_path.join("\u0000")
  ) {
    return "";
  }
  return current.section_path.join(" / ") || "正文";
}

/** 在原文覆盖视图原位打开候选详情弹窗，保留当前审核位置。 */
function openSourceCandidateDetail(candidateId: string): void {
  sourceCandidateDetailId.value = candidateId;
}

/** 关闭原文覆盖视图中的候选详情弹窗。 */
function closeSourceCandidateDetail(): void {
  sourceCandidateDetailId.value = "";
}

/** 把试抽取内部状态码转换成用户可读的中文状态。 */
function previewStatusLabel(status: PreviewRunSummary["status"]): string {
  return {
    queued: "等待中",
    running: "抽取中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  }[status];
}

/** 把质量校验基准状态转换成用户可读文本。 */
function checklistStatusLabel(status: DocumentChecklist["status"]): string {
  return status === "frozen" ? "已冻结" : "草稿";
}

/** 把比较会话状态转换成用户可读文本。 */
function comparisonStatusLabel(status: ProfileComparison["status"]): string {
  return status === "concluded" ? "已得出结论" : "待确认";
}

/** 把比较项在单侧的匹配状态转换成用户可读文本。 */
function comparisonItemStatusLabel(
  status: "matched" | "missing" | "uncertain_match",
): string {
  return {
    matched: "已命中",
    missing: "未命中",
    uncertain_match: "待确认",
  }[status];
}

/** 把校验项优先级转换成中文主标签，同时保留数据值语义。 */
function checklistPriorityLabel(priority: ChecklistPriority): string {
  return {
    must: "必须",
    should: "应该",
    nice_to_have: "最好有",
    noise: "噪声",
  }[priority];
}

/** 把比较结论代码转换成用户可读文本。 */
function comparisonOutcomeLabel(outcome: ComparisonOutcome): string {
  return {
    a_better: "Profile A 更好",
    b_better: "Profile B 更好",
    equivalent: "两者基本相当",
    both_insufficient: "两者都不够好",
  }[outcome];
}

/** 使用本地化千分位展示 token 等大整数。 */
function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

/** 根据拉丁字母和中文占比判断证据正文是否需要中文辅助翻译。 */
function requiresChineseTranslation(text: string): boolean {
  const latinCount = text.match(/[A-Za-z]/g)?.length ?? 0;
  const chineseCount = text.match(/[\u3400-\u9fff]/g)?.length ?? 0;
  return latinCount >= 20 && latinCount > chineseCount * 3;
}

/** 把文档 ID 转成当前 collection 中可读标题。 */
function documentTitle(docId: string): string {
  return documents.value.find((document) => document.doc_id === docId)?.title ?? docId;
}

/** 从当前文档 blocks 中解析校验项引用的原文证据。 */
function checklistEvidenceBlocks(sourceBlockIds: string[]): DocumentDetailBlock[] {
  return checklistBlocks.value.filter((block) => sourceBlockIds.includes(block.block_id));
}

/** 把字符串数组转换成逐行文本框值。 */
function lineText(values: string[]): string {
  return values.join("\n");
}

/** 把逐行文本框值解析成去空字符串数组。 */
function parseLines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 复制 Vue 响应式数据为普通对象，避免 structuredClone 拒绝 Proxy。 */
function cloneData<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 用文本框当前内容原位更新响应式字符串数组。 */
function replaceLines(target: string[], event: Event): void {
  target.splice(0, target.length, ...parseLines((event.target as HTMLTextAreaElement).value));
}

/** 创建可直接编辑的最小 Profile 配置。 */
function blankConfig(): ExtractionProfileConfig {
  return {
    name: "新建 Profile",
    description: "",
    domain_context: "",
    primary_uses: [],
    representative_questions: [],
    extraction_channels: [
      {
        channel_id: newChannelId(),
        name: "核心知识对象",
        purpose: "发现正文中能够独立学习和检索的知识对象。",
        activation: "正文对该对象提供定义、作用、条件、效果或边界时启用。",
        include_criteria: ["存在稳定名称", "正文提供独立事实"],
        exclude_criteria: ["只有标题、导航或顺带提及"],
        granularity_rule: "一个可独立学习和检索的对象对应一个候选。",
        coverage_audit_enabled: false,
      },
    ],
    exclude_criteria: [],
    granularity: "standard",
    additional_guidance: "",
    language_policy: {
      name: "preserve_source_term",
      summary: "zh-CN",
    },
  };
}

/** 重新读取 Profile 列表和 selected 指针。 */
async function loadProfiles(): Promise<void> {
  const [response, selected] = await Promise.all([
    client.listProfiles(),
    client.selection(),
  ]);
  profiles.value = response.profiles;
  selection.value = selected;
  const availableProfileIds = new Set(profiles.value.map((profile) => profile.profile_id));
  if (!previewProfileId.value || !availableProfileIds.has(previewProfileId.value)) {
    previewProfileId.value = selected.selected_profile_id ?? "";
  }
}

/** 读取当前 collection snapshot 中可预览的文档。 */
async function loadDocuments(): Promise<void> {
  const current = await client.currentCollection();
  documents.value = current?.manifest.documents ?? [];
  snapshotId.value = current?.manifest.snapshot_id ?? "";
}

/** 刷新 Preview Run 历史。 */
async function loadPreviewRuns(): Promise<void> {
  previewRuns.value = await client.listPreviewRuns();
}

/** 刷新文档清单历史。 */
async function loadChecklists(): Promise<void> {
  checklists.value = await client.listChecklists();
}

/** 刷新 Profile 比较历史。 */
async function loadComparisons(): Promise<void> {
  comparisons.value = await client.listComparisons();
}

/** 并行加载 Profile 工作区的全部基础数据。 */
async function loadAll(): Promise<void> {
  busy.value = true;
  setFeedback();
  try {
    await Promise.all([
      loadProfiles(),
      loadDocuments(),
      loadPreviewRuns(),
      loadChecklists(),
      loadComparisons(),
    ]);
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 在编辑器中打开 Profile 的深拷贝，避免未保存修改污染列表。 */
function openProfile(
  profile: ExtractionProfile,
  readOnly: boolean,
): void {
  setFeedback();
  try {
    editingProfile.value = profile;
    editingConfig.value = cloneData(profile.config);
    editingReadOnly.value =
      readOnly || profile.is_system || profile.status === "frozen";
    validationMessages.value = [];
    compiledPrompts.value = [];
    showCompiledPrompts.value = false;
    strategySuggestion.value = null;
    clearChannelAttributeSuggestions();
  } catch (caught) {
    setFeedback("", `打开 Profile 详情失败：${String(caught)}`);
  }
}

/** 关闭 Profile 详情弹窗并清除本次未保存的编辑副本。 */
function closeProfileEditor(): void {
  editingProfile.value = null;
  editingConfig.value = null;
  editingReadOnly.value = false;
  validationMessages.value = [];
  compiledPrompts.value = [];
  showCompiledPrompts.value = false;
  strategySuggestion.value = null;
  clearChannelAttributeSuggestions();
}

/** 把当前草稿详情从只读查看切换为可编辑模式。 */
function beginProfileEditing(): void {
  if (
    editingProfile.value?.status === "draft" &&
    !editingProfile.value.is_system
  ) {
    editingReadOnly.value = false;
  }
}

/** 创建一个新的空白 Profile draft。 */
async function createBlankProfile(): Promise<void> {
  busy.value = true;
  try {
    const created = await client.createProfile("新建 Profile");
    await loadProfiles();
    const fresh = profiles.value.find((profile) => profile.profile_id === created.profile_id);
    if (fresh) {
      openProfile(fresh, false);
    }
    setFeedback("已创建 Profile 草稿。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 从只读或冻结 Profile 创建可编辑副本。 */
async function cloneProfile(profile: ExtractionProfile): Promise<void> {
  busy.value = true;
  try {
    const cloned = await client.cloneProfile(profile.profile_id);
    await loadProfiles();
    const fresh = profiles.value.find((profile) => profile.profile_id === cloned.profile_id);
    if (fresh) {
      openProfile(fresh, false);
    }
    setFeedback("已创建可编辑副本。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求确认后删除 Profile 草稿。 */
function requestDeleteProfile(profile: ExtractionProfile): void {
  requestConfirmation({
    title: `删除“${profile.config.name}”？`,
    message: "此操作会永久删除这份 Profile 草稿，无法恢复。",
    confirmLabel: "删除草稿",
    isDangerous: true,
    onConfirm: async () => {
      await deleteProfile(profile);
    },
  });
}

/** 执行 Profile 草稿删除。 */
async function deleteProfile(profile: ExtractionProfile): Promise<void> {
  busy.value = true;
  try {
    await client.deleteProfile(profile.profile_id);
    editingProfile.value = null;
    editingConfig.value = null;
    await loadProfiles();
    setFeedback("草稿已删除。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 在当前草稿中添加一个空抽取通道。 */
function addChannel(): void {
  if (!editingConfig.value || editingConfig.value.extraction_channels.length >= 8) {
    return;
  }
  editingConfig.value.extraction_channels.push({
    channel_id: newChannelId(),
    name: "",
    purpose: "",
    activation: "",
    include_criteria: [],
    exclude_criteria: [],
    granularity_rule: "",
    coverage_audit_enabled: false,
  });
}

/** 从当前草稿删除指定位置的抽取通道。 */
function removeChannel(index: number): void {
  const [removed] = editingConfig.value?.extraction_channels.splice(index, 1) ?? [];
  if (removed) {
    delete channelAttributeSuggestions[removed.channel_id];
  }
}

/** 全量保存当前 Profile 草稿。 */
async function saveProfileDraft(): Promise<void> {
  if (!editingProfile.value || !editingConfig.value || editingReadOnly.value) {
    return;
  }
  busy.value = true;
  try {
    const saved = await client.saveProfile(editingProfile.value.profile_id, editingConfig.value);
    await loadProfiles();
    const fresh = profiles.value.find((profile) => profile.profile_id === saved.profile_id);
    if (fresh) {
      editingProfile.value = fresh;
    }
    setFeedback("草稿已保存。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 保存后执行 Profile 冻结前确定性校验。 */
async function validateProfile(): Promise<void> {
  if (!editingProfile.value || !editingConfig.value) {
    return;
  }
  busy.value = true;
  try {
    if (!editingReadOnly.value) {
      editingProfile.value = await client.saveProfile(
        editingProfile.value.profile_id,
        editingConfig.value,
      );
      await loadProfiles();
    }
    const result = await client.validateProfile(editingProfile.value.profile_id);
    validationMessages.value = result.issues.map((issue) => issue.message);
    setFeedback(result.valid ? "Profile 校验通过。" : "Profile 尚未完成。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 打开选中 Profile 的不可逆确认对话框。 */
function requestProfileSelection(profile: ExtractionProfile): void {
  pendingSelectionProfile.value = profile;
  showSelectionDialog.value = true;
}

/** 执行确认对话框中的 Profile 冻结和 selected 切换。 */
async function executeProfileSelection(): Promise<void> {
  const profile = pendingSelectionProfile.value;
  if (!profile) {
    return;
  }
  busy.value = true;
  try {
    selection.value = await client.selectProfile(profile.profile_id);
    await loadProfiles();
    showSelectionDialog.value = false;
    pendingSelectionProfile.value = null;
    closeProfileEditor();
    setFeedback("已切换当前使用的 Profile。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 保存编辑器内容，再执行确定使用流程。 */
async function saveAndSelectEditingProfile(): Promise<void> {
  if (!editingProfile.value || !editingConfig.value || editingReadOnly.value) {
    return;
  }
  busy.value = true;
  try {
    const saved = await client.saveProfile(editingProfile.value.profile_id, editingConfig.value);
    editingProfile.value = saved;
    await loadProfiles();
    busy.value = false;
    requestProfileSelection(saved);
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 读取当前已保存 Profile 的只读编译 Prompt。 */
async function showPrompts(): Promise<void> {
  if (!editingProfile.value) {
    return;
  }
  busy.value = true;
  try {
    compiledPrompts.value = await client.compiledPrompts(editingProfile.value.profile_id);
    showCompiledPrompts.value = true;
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求模型生成待审抽取通道草案。 */
async function generateStrategy(): Promise<void> {
  if (!editingConfig.value || editingReadOnly.value) {
    return;
  }
  if (!editingConfig.value.primary_uses.length) {
    setFeedback("", "请至少填写一项主要用途后再生成抽取策略。");
    return;
  }
  busy.value = true;
  try {
    const response = await client.suggestStrategy(
      editingConfig.value.name,
      editingConfig.value.primary_uses,
      editingConfig.value.representative_questions,
      editingConfig.value.domain_context,
    );
    strategySuggestion.value = {
      channels: response.channels,
      exclude_criteria: response.exclude_criteria,
    };
    setFeedback("已生成待审策略建议，尚未覆盖现有配置。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 在用户确认后把策略建议应用到本地草稿。 */
function applyStrategySuggestion(): void {
  if (!editingConfig.value || !strategySuggestion.value) {
    return;
  }
  editingConfig.value.extraction_channels = cloneData(strategySuggestion.value.channels);
  editingConfig.value.exclude_criteria = [...strategySuggestion.value.exclude_criteria];
  strategySuggestion.value = null;
  clearChannelAttributeSuggestions();
  setFeedback("策略建议已应用到当前草稿，请保存。");
}

/** 名称或目的变化后废弃该通道此前生成的其余属性建议。 */
function invalidateChannelAttributeSuggestion(channel: ExtractionChannel): void {
  delete channelAttributeSuggestions[channel.channel_id];
}

/** 根据当前通道名称和目的生成其余属性建议，不直接覆盖草稿。 */
async function generateChannelAttributes(channel: ExtractionChannel): Promise<void> {
  if (!editingConfig.value || editingReadOnly.value) {
    return;
  }
  const submittedName = channel.name.trim();
  const submittedPurpose = channel.purpose.trim();
  if (!submittedName || !submittedPurpose) {
    setFeedback("", "请先填写通道名称和目的。");
    return;
  }
  generatingChannelId.value = channel.channel_id;
  delete channelAttributeSuggestions[channel.channel_id];
  try {
    const suggestion = await client.suggestChannelAttributes(
      editingConfig.value.name,
      editingConfig.value.primary_uses,
      editingConfig.value.representative_questions,
      editingConfig.value.domain_context,
      submittedName,
      submittedPurpose,
    );
    const currentChannel = editingConfig.value?.extraction_channels.find(
      (candidate) => candidate.channel_id === channel.channel_id,
    );
    if (
      !currentChannel ||
      currentChannel.name.trim() !== submittedName ||
      currentChannel.purpose.trim() !== submittedPurpose
    ) {
      delete channelAttributeSuggestions[channel.channel_id];
      setFeedback("", "生成期间通道名称或目的已变化，请重新生成。");
      return;
    }
    channelAttributeSuggestions[channel.channel_id] = suggestion;
    setFeedback("已生成通道属性建议，确认后再应用。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    if (generatingChannelId.value === channel.channel_id) {
      generatingChannelId.value = "";
    }
  }
}

/** 将已审核的单通道建议写入草稿，同时保留名称、目的和其他通道。 */
function applyChannelAttributeSuggestion(channel: ExtractionChannel): void {
  const suggestion = channelAttributeSuggestions[channel.channel_id];
  if (!suggestion) {
    return;
  }
  channel.activation = suggestion.activation;
  channel.include_criteria = [...suggestion.include_criteria];
  channel.exclude_criteria = [...suggestion.exclude_criteria];
  channel.granularity_rule = suggestion.granularity_rule;
  channel.coverage_audit_enabled = suggestion.coverage_audit_enabled;
  delete channelAttributeSuggestions[channel.channel_id];
  setFeedback("通道属性建议已应用到当前草稿，请保存。");
}

/** 添加或移除一个 Preview 样本文档。 */
function toggleDocument(docId: string): void {
  previewDocumentIds.value = previewDocumentIds.value.includes(docId)
    ? previewDocumentIds.value.filter((value) => value !== docId)
    : [...previewDocumentIds.value, docId];
  previewPlan.value = null;
}

/** 选择当前知识库中的全部文档，供批量试抽取使用。 */
function selectAllPreviewDocuments(): void {
  previewDocumentIds.value = documents.value.map((document) => document.doc_id);
  previewPlan.value = null;
}

/** 清空试抽取文档选择。 */
function clearPreviewDocuments(): void {
  previewDocumentIds.value = [];
  previewPlan.value = null;
}

/** 在真正消耗模型 tokens 前计算预览计划。 */
async function calculatePreviewPlan(): Promise<void> {
  if (!previewProfileId.value || !previewDocumentIds.value.length || !snapshotId.value) {
    setFeedback("", "请选择 Profile 和至少一篇文档。");
    return;
  }
  busy.value = true;
  try {
    previewPlan.value = await client.planPreview(
      previewProfileId.value,
      snapshotId.value,
      previewDocumentIds.value,
    );
    setFeedback("试抽取成本估算已生成。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 展示成本提示并在用户确认后创建 Preview Run。 */
async function startPreview(): Promise<void> {
  if (!previewPlan.value) {
    await calculatePreviewPlan();
  }
  if (!previewPlan.value || !snapshotId.value) {
    return;
  }
  const plan = previewPlan.value;
  const reuseText = plan.reusable_run_id
    ? `将复用已有结果：${plan.reusable_run_id}`
    : "没有相同配置的成功结果，将创建新的试抽取记录。";
  const profileId = previewProfileId.value;
  const currentSnapshotId = snapshotId.value;
  const documentIds = [...previewDocumentIds.value];
  const forceNew = forceNewPreview.value;
  requestConfirmation({
    title: "开始 Profile 试抽取？",
    message: "系统将调用模型处理所选文档。试抽取不会生成 Wiki 页面或修改 Profile。",
    details: [
      `预计总 token：${formatNumber(plan.estimated_total_tokens)}`,
      `文档：${plan.document_ids.length} 篇`,
      `Blocks：${plan.block_count}`,
      `窗口：${plan.window_count}`,
      `预计模型调用：${plan.estimated_model_calls}`,
      `预计输入 token：${formatNumber(plan.estimated_input_tokens)}`,
      `预计输出 token：${formatNumber(plan.estimated_output_tokens)}`,
      reuseText,
    ],
    confirmLabel: "开始试抽取",
    onConfirm: async () => {
      busy.value = true;
      try {
        const run = await client.createPreview(
          profileId,
          currentSnapshotId,
          documentIds,
          forceNew,
        );
        activePreview.value = run;
        activePreviewCandidates.value = run.candidates;
        selectedPreviewCandidateId.value = run.candidates[0]?.candidate_id ?? "";
        resetPreviewCandidateFilters();
        resetSourceOrderWorkspace();
        evidenceTranslationNotice.value = "";
        await loadPreviewRuns();
        setFeedback(run.reused ? "已复用现有试抽取记录。" : "试抽取已进入队列。");
        startPolling();
      } finally {
        busy.value = false;
      }
    },
  });
}

/** 打开一个历史 Preview Run 及正式候选。 */
async function openPreviewRun(run: PreviewRunSummary): Promise<void> {
  busy.value = true;
  evidenceTranslationNotice.value = "";
  resetPreviewCandidateFilters();
  resetSourceOrderWorkspace();
  try {
    activePreview.value = await client.getPreviewRun(run.run_id);
    if (activePreview.value.status === "completed") {
      const page = await client.previewCandidates(run.run_id);
      activePreviewCandidates.value = page.candidates;
      selectedPreviewCandidateId.value = page.candidates[0]?.candidate_id ?? "";
    } else {
      activePreviewCandidates.value = [];
      selectedPreviewCandidateId.value = "";
    }
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 立即开始翻译当前试抽取中尚未翻译的英文 evidence。 */
function requestTranslateEvidence(): void {
  const blocks = untranslatedEnglishEvidence.value;
  if (!activePreview.value || !blocks.length) {
    setFeedback("", "当前没有需要翻译的英文证据。");
    return;
  }
  void translateActiveEvidence(blocks);
}

/** 调用后端翻译证据块，并合并到当前候选展示。 */
async function translateActiveEvidence(
  blocks: TranslatableSourceBlock[],
): Promise<void> {
  if (!activePreview.value || !blocks.length || evidenceTranslationBusy.value) {
    return;
  }
  evidenceTranslationBusy.value = true;
  evidenceTranslationNotice.value = `正在翻译 ${blocks.length} 个英文原文块…`;
  try {
    const response = await client.translateEvidence(
      activePreview.value.run_id,
      blocks.map((block) => block.block_id),
    );
    for (const item of response.items) {
      activePreviewTranslations[item.block_id] = item.translated_text;
    }
    evidenceTranslationNotice.value =
      `已获得 ${response.translated_count} 个中文翻译` +
      (response.reused_count ? `，其中 ${response.reused_count} 个复用了缓存。` : "。");
  } catch (caught) {
    evidenceTranslationNotice.value = `翻译失败：${String(caught)}`;
    setFeedback("", String(caught));
  } finally {
    evidenceTranslationBusy.value = false;
  }
}

/** 请求取消当前 queued 或 running Preview Run。 */
async function cancelActivePreview(): Promise<void> {
  if (!activePreview.value) {
    return;
  }
  busy.value = true;
  try {
    await client.cancelPreview(activePreview.value.run_id);
    activePreview.value = await client.getPreviewRun(activePreview.value.run_id);
    await loadPreviewRuns();
    setFeedback("已请求取消试抽取。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求确认后删除未被引用的终态试抽取记录。 */
function requestDeleteActivePreview(): void {
  if (!activePreview.value) {
    return;
  }
  requestConfirmation({
    title: "删除这条试抽取记录？",
    message: "记录及其本地候选会被永久删除；被比较记录引用时，系统会拒绝删除。",
    confirmLabel: "删除记录",
    isDangerous: true,
    onConfirm: async () => {
      await deleteActivePreview();
    },
  });
}

/** 执行终态试抽取记录删除。 */
async function deleteActivePreview(): Promise<void> {
  if (!activePreview.value) {
    return;
  }
  busy.value = true;
  try {
    await client.deletePreview(activePreview.value.run_id);
    activePreview.value = null;
    activePreviewCandidates.value = [];
    selectedPreviewCandidateId.value = "";
    resetSourceOrderWorkspace();
    await loadPreviewRuns();
    setFeedback("试抽取记录已删除。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 启动单实例 Preview 状态轮询。 */
function startPolling(): void {
  if (pollHandle.value !== null) {
    return;
  }
  pollHandle.value = window.setInterval(() => {
    void pollActivePreview();
  }, 900);
}

/** 刷新当前 Preview Run 状态和完成后的候选。 */
async function pollActivePreview(): Promise<void> {
  if (!activePreview.value) {
    return;
  }
  try {
    const run = await client.getPreviewRun(activePreview.value.run_id);
    activePreview.value = run;
    if (run.status === "completed") {
      const page = await client.previewCandidates(run.run_id);
      activePreviewCandidates.value = page.candidates;
      selectedPreviewCandidateId.value = page.candidates[0]?.candidate_id ?? "";
    }
    await loadPreviewRuns();
    if (["completed", "failed", "cancelled", "interrupted"].includes(run.status)) {
      stopPolling();
    }
  } catch (caught) {
    stopPolling();
    setFeedback("", String(caught));
  }
}

/** 停止 Preview 状态轮询。 */
function stopPolling(): void {
  if (pollHandle.value !== null) {
    window.clearInterval(pollHandle.value);
    pollHandle.value = null;
  }
}

/** 把一个 Preview 候选加入对应文档的清单草稿。 */
async function appendCandidateToChecklist(candidate: PreviewCandidate): Promise<boolean> {
  if (!activePreview.value) {
    return false;
  }
  busy.value = true;
  try {
    let checklist =
      checklists.value.find(
        (item) =>
          item.doc_id === candidate.doc_id &&
          item.snapshot_id === activePreview.value?.snapshot_id &&
          item.status === "draft",
      ) ?? null;
    if (!checklist) {
      checklist = await client.createChecklist(
        activePreview.value.snapshot_id,
        candidate.doc_id,
        `比较 Profile：${profileNameForRun(activePreview.value)}`,
      );
    }
    const item: DocumentChecklistItem = {
      item_id: newChecklistItemId(),
      name: candidate.name,
      aliases: [...candidate.aliases],
      priority: previewCandidatePriority.value,
      source_block_ids: [...candidate.source_block_ids],
      origin: "from_preview",
      origin_profile_id: activePreview.value.profile_id,
      origin_preview_run_id: activePreview.value.run_id,
    };
    checklist = await client.saveChecklist(checklist.checklist_id, checklist.evaluation_note, [
      ...checklist.items,
      item,
    ]);
    await loadChecklists();
    openChecklist(checklist);
    setFeedback("候选已加入该文档质量校验基准草稿。");
    return true;
  } catch (caught) {
    setFeedback("", String(caught));
    return false;
  } finally {
    busy.value = false;
  }
}

/** 从原文覆盖弹窗加入质量校验基准，成功后关闭弹窗并保留阅读位置。 */
async function addSourceCandidateToChecklist(candidate: PreviewCandidate): Promise<void> {
  if (await appendCandidateToChecklist(candidate)) {
    closeSourceCandidateDetail();
  }
}

/** 读取清单文档的 evidence block 选项。 */
async function loadChecklistBlocks(
  docId: string,
  checklistSnapshotId = snapshotId.value,
): Promise<void> {
  checklistBlocks.value =
    docId && checklistSnapshotId
      ? await client.checklistBlocks(checklistSnapshotId, docId)
      : [];
  manualItemBlockId.value = checklistBlocks.value[0]?.block_id ?? "";
}

/** 为当前文档创建空清单草稿。 */
async function createEmptyChecklist(): Promise<void> {
  if (!snapshotId.value || !checklistDocId.value) {
    setFeedback("", "请选择文档。");
    return;
  }
  busy.value = true;
  try {
    const checklist = await client.createChecklist(
      snapshotId.value,
      checklistDocId.value,
      checklistNote.value,
    );
    await loadChecklists();
    openChecklist(checklist);
    setFeedback("已创建质量校验基准草稿。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 生成清单建议并保存为新草稿，供用户编辑。 */
async function suggestChecklistItems(): Promise<void> {
  if (!snapshotId.value || !checklistDocId.value) {
    setFeedback("", "请选择文档。");
    return;
  }
  busy.value = true;
  try {
    const response = await client.suggestChecklist(
      snapshotId.value,
      checklistDocId.value,
      checklistNote.value,
    );
    const checklist = await client.createChecklist(
      snapshotId.value,
      checklistDocId.value,
      checklistNote.value,
    );
    const items: DocumentChecklistItem[] = response.items.map((item) => ({
      ...item,
      item_id: newChecklistItemId(),
      origin: "system_suggested",
      origin_profile_id: null,
      origin_preview_run_id: null,
    }));
    const saved = await client.saveChecklist(checklist.checklist_id, checklistNote.value, items);
    await loadChecklists();
    openChecklist(saved);
    setFeedback("校验项建议已生成到基准草稿，请检查后保存或冻结。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 打开清单并同步本地可编辑副本和 evidence 选项。 */
function openChecklist(checklist: DocumentChecklist): void {
  activeChecklist.value = checklist;
  checklistDocId.value = checklist.doc_id;
  checklistNote.value = checklist.evaluation_note;
  checklistItems.value = cloneData(checklist.items);
  void loadChecklistBlocks(checklist.doc_id, checklist.snapshot_id);
}

/** 把用户手工填写且绑定 evidence 的项目加入清单。 */
function addManualChecklistItem(): void {
  if (!manualItemName.value.trim() || !manualItemBlockId.value) {
    setFeedback("", "手工校验项需要名称和原文证据。");
    return;
  }
  checklistItems.value.push({
    item_id: newChecklistItemId(),
    name: manualItemName.value.trim(),
    aliases: [],
    priority: manualItemPriority.value,
    source_block_ids: [manualItemBlockId.value],
    origin: "manual",
    origin_profile_id: null,
    origin_preview_run_id: null,
  });
  manualItemName.value = "";
}

/** 保存当前仍可编辑的文档清单。 */
async function saveActiveChecklist(): Promise<void> {
  if (!activeChecklist.value || activeChecklist.value.status !== "draft") {
    return;
  }
  busy.value = true;
  try {
    const saved = await client.saveChecklist(
      activeChecklist.value.checklist_id,
      checklistNote.value,
      checklistItems.value,
    );
    await loadChecklists();
    openChecklist(saved);
    setFeedback("质量校验基准草稿已保存。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求确认后冻结当前质量校验基准。 */
function requestFreezeActiveChecklist(): void {
  if (!activeChecklist.value) {
    return;
  }
  requestConfirmation({
    title: "冻结这份质量校验基准？",
    message: "冻结后不能原地修改，并会被用于正式的 Profile 比较。",
    details: [`当前校验项：${checklistItems.value.length} 项`],
    confirmLabel: "冻结基准",
    onConfirm: async () => {
      await freezeActiveChecklist();
    },
  });
}

/** 保存并冻结当前文档清单。 */
async function freezeActiveChecklist(): Promise<void> {
  if (!activeChecklist.value) {
    return;
  }
  busy.value = true;
  try {
    const saved = await client.saveChecklist(
      activeChecklist.value.checklist_id,
      checklistNote.value,
      checklistItems.value,
    );
    const frozen = await client.freezeChecklist(saved.checklist_id);
    await loadChecklists();
    openChecklist(frozen);
    setFeedback("质量校验基准已冻结，可用于正式比较。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求确认后删除未冻结的质量校验基准草稿。 */
function requestDeleteActiveChecklist(): void {
  if (!activeChecklist.value) {
    return;
  }
  requestConfirmation({
    title: "删除这份质量校验基准草稿？",
    message: "未冻结的基准和当前校验项会被永久删除。",
    confirmLabel: "删除草稿",
    isDangerous: true,
    onConfirm: async () => {
      await deleteActiveChecklist();
    },
  });
}

/** 执行质量校验基准草稿删除。 */
async function deleteActiveChecklist(): Promise<void> {
  if (!activeChecklist.value) {
    return;
  }
  busy.value = true;
  try {
    await client.deleteChecklist(activeChecklist.value.checklist_id);
    activeChecklist.value = null;
    checklistItems.value = [];
    await loadChecklists();
    setFeedback("质量校验基准草稿已删除。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 添加或移除一个参与比较的冻结清单。 */
function toggleComparisonChecklist(checklistId: string): void {
  selectedChecklistIds.value = selectedChecklistIds.value.includes(checklistId)
    ? selectedChecklistIds.value.filter((value) => value !== checklistId)
    : [...selectedChecklistIds.value, checklistId];
}

/** 校验选择并创建逐文档 Profile 比较。 */
async function createComparison(): Promise<void> {
  if (
    !comparisonRunAId.value ||
    !comparisonRunBId.value ||
    !selectedChecklistIds.value.length
  ) {
    setFeedback("", "请选择两个 completed run 和对应的冻结质量校验基准。");
    return;
  }
  busy.value = true;
  try {
    const [runA, runB, comparison] = await Promise.all([
      client.getPreviewRun(comparisonRunAId.value),
      client.getPreviewRun(comparisonRunBId.value),
      client.createComparison(
        comparisonRunAId.value,
        comparisonRunBId.value,
        selectedChecklistIds.value,
      ),
    ]);
    comparisonRunA.value = runA;
    comparisonRunB.value = runB;
    activeComparison.value = comparison;
    comparisons.value = await client.listComparisons();
    for (const document of comparison.document_results) {
      mustOverrideReasons[document.doc_id] ??= "";
      documentOutcomes[document.doc_id] ??= comparisonOutcome.value;
    }
    setFeedback("比较已生成。请检查 uncertain 项和 must 提示后提交结论。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 打开历史比较及其对应两个 Preview Run。 */
async function loadComparison(comparison: ProfileComparison): Promise<void> {
  busy.value = true;
  try {
    activeComparison.value = comparison;
    comparisonRunAId.value = comparison.run_a_id;
    comparisonRunBId.value = comparison.run_b_id;
    comparisonRunA.value = await client.getPreviewRun(comparison.run_a_id);
    comparisonRunB.value = await client.getPreviewRun(comparison.run_b_id);
    for (const document of comparison.document_results) {
      mustOverrideReasons[document.doc_id] = document.must_override_reason ?? "";
      documentOutcomes[document.doc_id] =
        document.user_outcome ?? "equivalent";
    }
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 读取某文档在 Profile A 或 B run 中的候选。 */
function sideCandidates(
  document: ComparisonDocumentResult,
  side: "a" | "b",
  candidateIds: string[],
): PreviewCandidate[] {
  const run = side === "a" ? comparisonRunA.value : comparisonRunB.value;
  return (
    run?.candidates.filter(
      (candidate) =>
        candidate.doc_id === document.doc_id &&
        candidateIds.includes(candidate.candidate_id),
    ) ?? []
  );
}

/** 把用户对 uncertain 项的确认写入比较结果。 */
async function resolveUncertain(
  itemId: string,
  side: "a" | "b",
  decision: "matched" | "missing",
): Promise<void> {
  if (!activeComparison.value) {
    return;
  }
  const key = `${itemId}:${side}`;
  const candidateId = decision === "matched" ? uncertainCandidateChoice[key] : null;
  if (decision === "matched" && !candidateId) {
    setFeedback("", "请先选择确认命中的 candidate。");
    return;
  }
  busy.value = true;
  try {
    activeComparison.value = await client.applyComparisonDecision(
      activeComparison.value.comparison_id,
      itemId,
      side,
      decision,
      candidateId,
    );
    setFeedback("人工匹配决定已记录。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 请求确认后提交总体比较结论。 */
function requestConcludeComparison(): void {
  if (!activeComparison.value) {
    return;
  }
  requestConfirmation({
    title: "提交并冻结比较结论？",
    message: "提交后这次比较不能再修改；后续调整需要创建新的比较。",
    details: [`总体结论：${comparisonOutcomeLabel(comparisonOutcome.value)}`],
    confirmLabel: "提交并冻结",
    onConfirm: async () => {
      await concludeComparison();
    },
  });
}

/** 提交总体比较结论并冻结 Comparison。 */
async function concludeComparison(): Promise<void> {
  if (!activeComparison.value) {
    return;
  }
  busy.value = true;
  try {
    activeComparison.value = await client.concludeComparison(
      activeComparison.value.comparison_id,
      comparisonOutcome.value,
      comparisonNote.value,
      { ...mustOverrideReasons },
      { ...documentOutcomes },
    );
    comparisons.value = await client.listComparisons();
    setFeedback("比较结论已提交并冻结。");
  } catch (caught) {
    setFeedback("", String(caught));
  } finally {
    busy.value = false;
  }
}

/** 把 Profile 持久状态转换成用户可读标签。 */
function profileStatusLabel(profile: ExtractionProfile): string {
  if (profile.is_system) {
    return "系统只读";
  }
  return profile.status === "draft"
    ? "草稿"
    : profile.status === "conflicted"
      ? "存在冲突"
      : "已冻结";
}

/** 判断 Profile 是否已有成功预览，用于确定前提示验证状态。 */
function hasCompletedPreview(profile: ExtractionProfile): boolean {
  return previewRuns.value.some(
    (run) => run.profile_id === profile.profile_id && run.status === "completed",
  );
}

onMounted(() => {
  void loadAll();
});

onBeforeUnmount(() => {
  stopPolling();
});
</script>

<template>
  <section class="profile-workbench">
    <div class="section-heading">
      <div>
        <p class="eyebrow">PROFILE EXTRACTION</p>
        <h2>Profile 工作台</h2>
        <p class="workbench-subtitle">
          定义抽取方式，小范围试跑验证，再用同一份基准比较结果。
        </p>
      </div>
      <span class="settings-pill" :class="{ configured: Boolean(selectedProfile) }">
        当前：{{ selectedProfileName }}
      </span>
    </div>

    <div class="profile-tabs" role="tablist" aria-label="Profile 抽取工作区">
      <button
        v-for="(tab, tabIndex) in [
          ['profiles', 'Profile 设置', '创建并确定抽取策略'],
          ['preview', '试抽取', '用少量文档检查结果'],
          ['checklists', '质量校验基准', '定义比较所依据的标准'],
          ['comparisons', 'Profile 比较', '对照基准选择更优方案'],
        ] as const"
        :key="tab[0]"
        type="button"
        :class="{ active: activeTab === tab[0] }"
        role="tab"
        :aria-selected="activeTab === tab[0]"
        @click="activeTab = tab[0]"
      >
        <span class="profile-tab-step">{{ tabIndex + 1 }}</span>
        <span class="profile-tab-copy">
          <strong>{{ tab[1] }}</strong>
          <small>{{ tab[2] }}</small>
        </span>
      </button>
    </div>

    <div class="profile-status-row">
      <div v-if="selectedProfile" class="active-profile-banner">
        <div>
          <span class="active-profile-mark">当前生效 Profile</span>
          <strong>{{ selectedProfile.config.name }}</strong>
        </div>
        <span>{{ selectedProfile.profile_id }} · {{ selectedProfile.profile_hash.slice(0, 12) }}</span>
      </div>
      <div v-else class="active-profile-warning">
        尚未设置当前 Profile，无法进行正式的全库 Wiki 构建
      </div>
      <button
        v-if="activeTab !== 'profiles'"
        type="button"
        @click="activeTab = 'profiles'"
      >
        {{ selectedProfile ? "切换 Profile" : "去设置 Profile" }}
      </button>
      <button
        v-if="activeTab === 'profiles'"
        type="button"
        class="primary-action profile-create-action"
        :disabled="busy"
        @click="createBlankProfile"
      >
        新建 Profile
      </button>
    </div>

    <p v-if="error" class="error-box">{{ error }}</p>
    <p v-if="message" class="settings-message">{{ message }}</p>

    <template v-if="activeTab === 'profiles'">
      <section class="tab-intro">
        <strong>从系统模板或空白配置开始</strong>
        <p>
          草稿可以反复修改；点击“确定使用”后会冻结并设为当前 Profile。
          需要继续调整时，复制它创建新的草稿即可。
        </p>
      </section>
      <div class="profile-grid">
        <article
          v-for="profile in profiles"
          :key="profile.profile_id"
          class="profile-card"
          :class="{
            'is-viewing': editingProfile?.profile_id === profile.profile_id,
            'is-selected': selection.selected_profile_id === profile.profile_id,
          }"
        >
          <div class="profile-card-heading">
            <div>
              <strong>{{ profile.config.name || "未命名 Profile" }}</strong>
              <small>{{ profileStatusLabel(profile) }}</small>
            </div>
            <span
              v-if="selection.selected_profile_id === profile.profile_id"
              class="selected-badge"
            >当前生效</span>
          </div>
          <p>{{ profile.config.description || "尚未填写 Profile 说明。" }}</p>
          <p class="profile-meta">
            {{ profile.config.extraction_channels.length }} 个通道 ·
            {{ profile.profile_hash.slice(0, 12) }}
          </p>
          <div class="profile-signals">
            <span
              class="profile-signal"
              :class="{ verified: hasCompletedPreview(profile) }"
            >
              {{ hasCompletedPreview(profile) ? "已通过试抽取" : "尚未试抽取" }}
            </span>
            <span class="profile-signal">
              {{ profile.config.primary_uses.length }} 项主要用途
            </span>
          </div>
          <div class="profile-actions">
            <button type="button" @click="openProfile(profile, true)">查看详情</button>
            <button
              v-if="profile.status === 'draft'"
              type="button"
              class="primary-action"
              @click="openProfile(profile, false)"
            >编辑</button>
            <button
              v-if="profile.is_system || profile.status !== 'draft'"
              type="button"
              @click="cloneProfile(profile)"
            >复制调整</button>
            <button
              v-if="selection.selected_profile_id !== profile.profile_id"
              type="button"
              class="selection-action"
              @click="requestProfileSelection(profile)"
            >
              <span
                class="info-tooltip button-info"
                tabindex="0"
                role="note"
                aria-label="确定使用的说明"
                title="该 Profile 将冻结且不可编辑；仍可切换其他 Profile，或复制后调整。"
                @click.stop.prevent
                @keydown.stop
              >
                i
                <span role="tooltip">
                  系统检查 Profile 是否完整，以及通道和排除规则是否存在直接冲突。检查通过后
                  冻结并设为当前 Profile，只更新当前选择，不执行试抽取或全库构建。
                  冻结后该 Profile 不可更改。
                </span>
              </span>
              <span>确定使用</span>
            </button>
            <button
              v-if="!profile.is_system && profile.status === 'draft'"
              type="button"
              class="danger-link"
              @click="requestDeleteProfile(profile)"
            >删除草稿</button>
          </div>
        </article>
      </div>

      <div
        v-if="editingConfig"
        class="dialog-backdrop profile-detail-backdrop"
        @click.self="closeProfileEditor"
      >
        <section class="profile-editor profile-detail-dialog">
          <div class="section-heading">
            <div>
              <p class="eyebrow">PROFILE EDITOR</p>
              <h3>{{ editingConfig.name || "未命名 Profile" }}</h3>
            </div>
            <div class="profile-editor-heading-actions">
              <button
                v-if="editingProfile && !editingReadOnly"
                type="button"
                class="primary-action"
                :disabled="busy"
                @click="saveProfileDraft"
              >保存草稿</button>
              <button
                v-if="
                  editingProfile?.status === 'draft' &&
                  !editingProfile.is_system &&
                  editingReadOnly
                "
                type="button"
                class="primary-action"
                @click="beginProfileEditing"
              >编辑</button>
              <button type="button" @click="closeProfileEditor">关闭</button>
            </div>
          </div>
          <p v-if="editingReadOnly" class="settings-description">
            {{
              editingProfile?.status === "draft"
                ? "当前为只读查看。点击右上角“编辑”后可修改此草稿。"
                : "系统或冻结 Profile 为只读。需要调整时请创建副本。"
            }}
          </p>

        <div class="profile-form-grid">
          <label>
            <span class="label-with-help">
              <span>Profile 名称</span>
              <FieldHelp
                aria-label="Profile 名称说明"
                text="用于识别这套抽取配置。名称不参与 candidate 身份，也不参与知识条目合并。"
              />
            </span>
            <input v-model="editingConfig.name" :disabled="editingReadOnly" />
          </label>
          <label>
            <span class="label-with-help">
              <span>领域或文档范围</span>
              <FieldHelp
                aria-label="领域说明"
                text="帮助系统理解术语和抽取重点的选填上下文。它不限制对象类型，也不替代抽取通道。"
              />
            </span>
            <input v-model="editingConfig.domain_context" :disabled="editingReadOnly" />
          </label>
          <label class="span-two">
            <span class="label-with-help">
              <span>Profile 说明</span>
              <FieldHelp
                aria-label="Profile 说明字段说明"
                text="向用户描述这套 Profile 面向什么材料、解决什么问题，不直接控制抽取行为。"
              />
            </span>
            <textarea v-model="editingConfig.description" :disabled="editingReadOnly" />
          </label>
          <label>
            <span class="label-with-help">
              <span>主要用途（每行一项）</span>
              <FieldHelp
                aria-label="主要用途字段说明"
                text="描述用户准备如何使用知识库，例如学习、查询操作、风险审查。可用于生成抽取通道建议。"
              />
            </span>
            <textarea
              :value="lineText(editingConfig.primary_uses)"
              :disabled="editingReadOnly"
              @input="replaceLines(editingConfig.primary_uses, $event)"
            />
          </label>
          <label>
            <span class="label-with-help">
              <span>代表性问题（每行一项）</span>
              <FieldHelp
                aria-label="代表性问题字段说明"
                text="提供用户希望知识库回答的典型问题，帮助系统生成更贴近用途的抽取通道草案。"
              />
            </span>
            <textarea
              :value="lineText(editingConfig.representative_questions)"
              :disabled="editingReadOnly"
              @input="replaceLines(editingConfig.representative_questions, $event)"
            />
          </label>
        </div>

        <section v-if="!editingReadOnly" class="strategy-generator">
          <div class="strategy-generator-copy">
            <div class="strategy-generator-title">
              <strong>根据用途生成抽取通道</strong>
              <FieldHelp
                aria-label="生成抽取通道建议说明"
                text="系统读取 Profile 名称、领域、主要用途和代表性问题，生成 1–8 个待审抽取通道及全局排除条件。不会自动保存，也不会直接替换当前配置。"
              />
            </div>
            <p>
              先填写上方“主要用途”。代表性问题可选填，但填写后生成结果会更贴近实际问答需求。
            </p>
          </div>
          <div class="strategy-generator-action">
            <button
              type="button"
              class="primary-action"
              :disabled="busy || editingConfig.primary_uses.length === 0"
              @click="generateStrategy"
            >
              {{ busy ? "生成中…" : "生成待审建议" }}
            </button>
            <small v-if="editingConfig.primary_uses.length === 0">
              请先填写至少一项主要用途
            </small>
          </div>
        </section>

        <section v-if="strategySuggestion" class="suggestion-panel strategy-suggestion-panel">
          <div class="channel-card-heading">
            <div>
              <strong>待审通道建议</strong>
              <small>尚未写入当前配置</small>
            </div>
            <span>{{ strategySuggestion.channels.length }} 个通道</span>
          </div>
          <ol>
            <li v-for="channel in strategySuggestion.channels" :key="channel.channel_id">
              <strong>{{ channel.name }}</strong>：{{ channel.purpose }}
            </li>
          </ol>
          <p class="strategy-suggestion-note">
            {{
              editingConfig.extraction_channels.length
                ? `应用后会替换当前 ${editingConfig.extraction_channels.length} 个通道和全局排除条件；保存草稿前请确认。`
                : "应用后会写入当前草稿的抽取通道和全局排除条件。"
            }}
          </p>
          <div class="strategy-suggestion-actions">
            <button type="button" @click="strategySuggestion = null">保留当前配置</button>
            <button type="button" class="primary-action" @click="applyStrategySuggestion">
              {{
                editingConfig.extraction_channels.length
                  ? "替换为这组建议"
                  : "应用到当前草稿"
              }}
            </button>
          </div>
        </section>

        <div class="channel-editor-heading">
          <div>
            <p class="eyebrow">EXTRACTION CHANNELS</p>
            <h3>抽取通道 <span class="section-count">{{ editingConfig.extraction_channels.length }}/8</span></h3>
          </div>
          <button
            v-if="!editingReadOnly"
            type="button"
            :disabled="editingConfig.extraction_channels.length >= 8"
            @click="addChannel"
          >添加通道</button>
        </div>

        <article
          v-for="(channel, index) in editingConfig.extraction_channels"
          :key="channel.channel_id"
          class="channel-card"
        >
          <div class="channel-card-heading">
            <div>
              <strong>通道 {{ index + 1 }} · {{ channel.name || "未命名通道" }}</strong>
              <small>{{ channel.purpose || "填写目的后会显示在这里" }}</small>
            </div>
            <button
              v-if="!editingReadOnly"
              type="button"
              class="danger-link"
              @click="removeChannel(index)"
            >删除</button>
          </div>
          <div class="profile-form-grid">
            <label>
              <span class="label-with-help">
                <span>名称</span>
                <FieldHelp
                  aria-label="通道名称说明"
                  text="用于在界面和组织中识别这个通道，不写入 candidate，也不参与候选身份合并。"
                />
              </span>
              <input
                v-model="channel.name"
                :disabled="editingReadOnly"
                @input="invalidateChannelAttributeSuggestion(channel)"
              />
            </label>
            <label>
              <span class="label-with-help">
                <span>目的</span>
                <FieldHelp
                  aria-label="通道目的说明"
                  text="说明这个通道单独负责发现什么知识对象。一个通道只应承担一个主要抽取视角。"
                />
              </span>
              <input
                v-model="channel.purpose"
                :disabled="editingReadOnly"
                @input="invalidateChannelAttributeSuggestion(channel)"
              />
            </label>
            <div v-if="!editingReadOnly" class="channel-ai-assist span-two">
              <div>
                <strong>补全其余通道属性</strong>
                <p>根据名称、目的和 Profile 上下文生成待审建议；不会改动名称和目的。</p>
              </div>
              <button
                type="button"
                :disabled="
                  busy ||
                  Boolean(generatingChannelId) ||
                  !channel.name.trim() ||
                  !channel.purpose.trim()
                "
                @click="generateChannelAttributes(channel)"
              >
                {{
                  generatingChannelId === channel.channel_id
                    ? "生成中…"
                    : "生成其余属性"
                }}
              </button>
            </div>
            <div
              v-if="channelAttributeSuggestions[channel.channel_id]"
              class="channel-attribute-suggestion span-two"
            >
              <div class="channel-card-heading">
                <div>
                  <strong>待审通道属性</strong>
                  <small>尚未写入当前通道</small>
                </div>
                <span>AI 建议</span>
              </div>
              <dl>
                <dt>语义触发条件</dt>
                <dd>{{ channelAttributeSuggestions[channel.channel_id].activation }}</dd>
                <dt>包含条件</dt>
                <dd>
                  {{
                    channelAttributeSuggestions[channel.channel_id].include_criteria.join("；") ||
                    "无"
                  }}
                </dd>
                <dt>通道排除条件</dt>
                <dd>
                  {{
                    channelAttributeSuggestions[channel.channel_id].exclude_criteria.join("；") ||
                    "无"
                  }}
                </dd>
                <dt>粒度规则</dt>
                <dd>{{ channelAttributeSuggestions[channel.channel_id].granularity_rule }}</dd>
                <dt>遗漏补抽</dt>
                <dd>
                  {{
                    channelAttributeSuggestions[channel.channel_id].coverage_audit_enabled
                      ? "开启"
                      : "关闭"
                  }}
                </dd>
              </dl>
              <div class="channel-attribute-actions">
                <button
                  type="button"
                  @click="delete channelAttributeSuggestions[channel.channel_id]"
                >
                  保留当前属性
                </button>
                <button
                  type="button"
                  class="primary-action"
                  @click="applyChannelAttributeSuggestion(channel)"
                >
                  应用其余属性
                </button>
              </div>
            </div>
            <label class="span-two">
              <span class="label-with-help">
                <span>语义触发条件</span>
                <FieldHelp
                  aria-label="语义触发条件说明"
                  text="描述正文出现什么语义条件时才应用该通道。该字段会编译进通道 Prompt，由模型根据当前证据判断，不是程序级硬过滤。"
                />
              </span>
              <textarea v-model="channel.activation" :disabled="editingReadOnly" />
            </label>
            <label>
              <span class="label-with-help">
                <span>包含条件（每行一项）</span>
                <FieldHelp
                  aria-label="包含条件说明"
                  text="候选需要同时满足的准入条件。它们约束模型判断，不会自动转化成 candidate 标签。"
                />
              </span>
              <textarea
                :value="lineText(channel.include_criteria)"
                :disabled="editingReadOnly"
                @input="replaceLines(channel.include_criteria, $event)"
              />
            </label>
            <label>
              <span class="label-with-help">
                <span>排除条件（每行一项）</span>
                <FieldHelp
                  aria-label="通道排除条件说明"
                  text="限定本通道不应抽取的内容。系统会检查与包含条件、全局排除规则之间的直接冲突。"
                />
              </span>
              <textarea
                :value="lineText(channel.exclude_criteria)"
                :disabled="editingReadOnly"
                @input="replaceLines(channel.exclude_criteria, $event)"
              />
            </label>
            <label class="span-two">
              <span class="label-with-help">
                <span>粒度规则</span>
                <FieldHelp
                  aria-label="粒度规则说明"
                  text="说明什么时候一个知识对象应独立成为候选。例如普通属性留在证据中，只有能独立检索的对象才成为 candidate。"
                />
              </span>
              <textarea v-model="channel.granularity_rule" :disabled="editingReadOnly" />
            </label>
            <label class="inline-check">
              <input
                v-model="channel.coverage_audit_enabled"
                type="checkbox"
                :disabled="editingReadOnly"
              />
              <span class="label-with-help">
                <span>启用遗漏补抽</span>
                <FieldHelp
                  aria-label="遗漏补抽说明"
                  text="在首次抽取后，用同一通道再检查一次是否有明确遗漏。它只补充候选，不重写已有结果，并增加一次模型调用。"
                />
              </span>
            </label>
          </div>
        </article>

        <details class="advanced-profile-settings">
          <summary>高级设置：全局排除、默认粒度和补充说明</summary>
          <div class="profile-form-grid">
            <label>
              <span class="label-with-help">
                <span>全局排除条件（每行一项）</span>
                <FieldHelp
                  aria-label="全局排除条件说明"
                  text="应用于 Profile 所有通道的排除规则，例如导航、页脚和宣传性套话。不能与通道包含条件直接冲突。"
                />
              </span>
              <textarea
                :value="lineText(editingConfig.exclude_criteria)"
                :disabled="editingReadOnly"
                @input="replaceLines(editingConfig.exclude_criteria, $event)"
              />
            </label>
            <label>
              <span class="label-with-help">
                <span>粒度倾向</span>
                <FieldHelp
                  aria-label="粒度倾向说明"
                  text="为所有通道提供默认粒度倾向。标准粒度强调可独立学习或检索，不代表自动删除较细候选。"
                />
              </span>
              <select v-model="editingConfig.granularity" :disabled="editingReadOnly">
                <option value="coarse">较粗</option>
                <option value="standard">标准</option>
                <option value="fine">较细</option>
              </select>
            </label>
            <label class="span-two">
              <span class="label-with-help">
                <span>补充说明</span>
                <FieldHelp
                  aria-label="补充说明字段说明"
                  text="填写结构化字段难以覆盖的领域约定。不能覆盖证据、输出 schema、alias 或失败契约。"
                />
              </span>
              <textarea
                v-model="editingConfig.additional_guidance"
                :disabled="editingReadOnly"
              />
            </label>
          </div>
        </details>

        <div class="profile-actions">
          <button type="button" :disabled="busy" @click="showPrompts">
            查看已保存的编译 Prompt
          </button>
          <button
            v-if="editingProfile"
            type="button"
            :disabled="busy || editingReadOnly"
            @click="validateProfile"
          >保存并校验</button>
          <button
            v-if="editingProfile"
            type="button"
            class="primary-action selection-action"
            :disabled="busy || editingReadOnly"
            @click="saveAndSelectEditingProfile"
          >
            <span
              class="info-tooltip button-info"
              tabindex="0"
              role="note"
              aria-label="保存并确定使用的说明"
              title="检查通过后冻结并设为当前 Profile；不会执行构建。"
              @click.stop.prevent
              @keydown.stop
            >
              i
              <span role="tooltip">
                系统检查 Profile 是否完整，以及通道和排除规则是否存在直接冲突。检查通过后
                冻结并设为当前 Profile，只更新当前选择，不执行试抽取或全库构建。
                冻结后该 Profile 不可更改。
              </span>
            </span>
            <span>保存并确定使用</span>
          </button>
        </div>

        <ul v-if="validationMessages.length" class="validation-list">
          <li v-for="item in validationMessages" :key="item">{{ item }}</li>
        </ul>

          <div v-if="showCompiledPrompts" class="compiled-prompts">
            <article v-for="prompt in compiledPrompts" :key="prompt.channel_id">
              <h4>{{ prompt.channel_name }}</h4>
              <pre>{{ prompt.prompt }}</pre>
            </article>
          </div>
        </section>
      </div>
    </template>

    <template v-if="activeTab === 'preview'">
      <section class="tab-intro">
        <strong>先小范围试抽取，再决定是否构建全库 Wiki</strong>
        <p>
          选择一个 Profile 和少量已入库文档，实际执行一轮抽取并检查候选与原文证据。
          这里不会生成 Wiki 页面，也不会修改 Profile 或当前知识库内容。
        </p>
      </section>
      <div class="preview-builder">
        <label>
          <span>Profile</span>
          <select v-model="previewProfileId" @change="previewPlan = null">
            <option value="">选择 Profile</option>
            <option
              v-for="profile in profiles"
              :key="profile.profile_id"
              :value="profile.profile_id"
            >{{ profile.config.name || "未命名 Profile" }}</option>
          </select>
        </label>
        <div class="document-picker">
          <div class="document-picker-heading">
            <div>
              <span class="field-label">选择试抽取文档</span>
              <small>已选 {{ previewDocumentIds.length }} / {{ documents.length }} 篇</small>
            </div>
            <div class="document-picker-actions">
              <button type="button" @click="selectAllPreviewDocuments">全选</button>
              <button type="button" @click="clearPreviewDocuments">清空</button>
            </div>
          </div>
          <div class="document-options">
            <label v-for="document in documents" :key="document.doc_id" class="document-check">
              <input
                type="checkbox"
                :checked="previewDocumentIds.includes(document.doc_id)"
                @change="toggleDocument(document.doc_id)"
              />
              <span>
                <strong>{{ document.title }}</strong>
                <small>{{ document.block_count }} 个 block</small>
              </span>
            </label>
          </div>
        </div>
        <label class="inline-check">
          <input v-model="forceNewPreview" type="checkbox" />
          <span>即使配置相同也强制创建新的试抽取记录</span>
        </label>
        <div class="profile-actions">
          <button type="button" :disabled="busy" @click="calculatePreviewPlan">
            估算 token 用量
          </button>
          <button
            type="button"
            class="primary-action"
            :disabled="busy || (previewPlan !== null && !previewPlan.provider_ready)"
            @click="startPreview"
          >
            开始试抽取
          </button>
        </div>
        <div v-if="previewPlan" class="preview-plan">
          <div class="plan-total">
            <span>预计总 token</span>
            <strong>{{ formatNumber(previewPlan.estimated_total_tokens) }}</strong>
            <small>输入与输出的粗略估算，实际用量以模型返回为准</small>
          </div>
          <div class="plan-details">
            <span>输入 {{ formatNumber(previewPlan.estimated_input_tokens) }}</span>
            <span>输出 {{ formatNumber(previewPlan.estimated_output_tokens) }}</span>
            <span>{{ previewPlan.estimated_model_calls }} 次模型调用</span>
            <span>{{ previewPlan.document_ids.length }} 篇文档 · {{ previewPlan.block_count }} blocks · {{ previewPlan.window_count }} 窗口</span>
          </div>
          <span v-if="previewPlan.reusable_run_id" class="plan-reuse">
            可直接复用现有试抽取记录：{{ previewPlan.reusable_run_id }}
          </span>
          <span v-if="!previewPlan.provider_ready" class="error-inline">
            尚未配置 Chat API Key，无法开始试抽取。
          </span>
        </div>
      </div>

      <div class="preview-history-heading">
        <div>
          <strong>试抽取记录</strong>
          <small>打开历史记录，或隐藏历史侧栏以扩大候选阅读区</small>
        </div>
        <button type="button" @click="showPreviewHistory = !showPreviewHistory">
          {{ showPreviewHistory ? "隐藏历史侧栏" : `显示历史（${previewRuns.length}）` }}
        </button>
      </div>
      <div
        class="preview-layout"
        :class="{ 'history-collapsed': !showPreviewHistory }"
      >
        <aside v-if="showPreviewHistory" class="preview-run-list">
          <div class="aside-heading">
            <strong>试抽取历史</strong>
            <span>{{ filteredPreviewRuns.length }} / {{ previewRuns.length }}</span>
          </div>
          <div class="history-filters">
            <input v-model="previewHistoryQuery" placeholder="搜索 Profile 或运行 ID" />
            <select v-model="previewHistoryStatus">
              <option value="">全部状态</option>
              <option value="queued">等待中</option>
              <option value="running">抽取中</option>
              <option value="completed">已完成</option>
              <option value="failed">失败</option>
              <option value="cancelled">已取消</option>
              <option value="interrupted">已中断</option>
            </select>
          </div>
          <p v-if="!previewRuns.length" class="aside-empty">
            还没有试抽取记录。选择文档并开始试抽取后，记录会出现在这里。
          </p>
          <p v-else-if="!filteredPreviewRuns.length" class="aside-empty">
            没有符合当前筛选条件的记录。
          </p>
          <button
            v-for="run in filteredPreviewRuns"
            :key="run.run_id"
            type="button"
            :class="{ active: activePreview?.run_id === run.run_id }"
            @click="openPreviewRun(run)"
          >
            <strong>{{ profileNameForRun(run) }}</strong>
            <span
              class="run-status-line"
              :class="{ running: ['queued', 'running'].includes(run.status) }"
            >{{ previewStatusLabel(run.status) }} · {{ run.document_scope.length }} 篇</span>
            <small>{{ new Date(run.created_at).toLocaleString() }}</small>
          </button>
        </aside>
        <div class="preview-detail">
          <template v-if="activePreview">
            <div class="channel-card-heading">
              <div>
                <strong>{{ profileNameForRun(activePreview) }}</strong>
                <small>{{ activePreview.run_id }}</small>
              </div>
              <span
                class="status-pill"
                :class="`status-${activePreview.status}`"
              >{{ previewStatusLabel(activePreview.status) }}</span>
            </div>
            <div
              v-if="['queued', 'running'].includes(activePreview.status)"
              class="extraction-progress"
              role="status"
              aria-live="polite"
            >
              <span class="extraction-spinner" aria-hidden="true"></span>
              <div>
                <strong>
                  {{
                    activePreview.status === "queued"
                      ? "已进入队列，等待开始"
                      : "正在试抽取"
                  }}
                </strong>
                <p>
                  {{
                    activePreview.status === "queued"
                      ? "前一个任务结束后会自动开始，请保持应用打开。"
                      : "模型正在逐篇、逐通道发现候选；完成后结果会自动出现。"
                  }}
                </p>
              </div>
            </div>
            <p v-if="activePreview.error_message" class="error-box">
              {{ activePreview.error_message }}
            </p>
            <div class="profile-actions">
              <button
                v-if="['queued', 'running'].includes(activePreview.status)"
                type="button"
                :disabled="busy"
                @click="cancelActivePreview"
              >取消</button>
              <button
                v-else
                type="button"
                class="danger-link"
                :disabled="busy"
                @click="requestDeleteActivePreview"
              >删除试抽取记录</button>
            </div>

            <div v-if="activePreview.status === 'completed'" class="preview-view-switch">
              <button
                type="button"
                :class="{ active: previewResultView === 'candidates' }"
                @click="showCandidateView"
              >
                候选浏览
              </button>
              <button
                type="button"
                :class="{ active: previewResultView === 'source-order' }"
                @click="showSourceOrderView"
              >
                按原文阅读
              </button>
            </div>

            <div
              v-if="
                activePreview.status === 'completed' &&
                previewResultView === 'candidates'
              "
              class="candidate-browser-toolbar"
            >
              <label>
                <span>搜索候选</span>
                <input v-model="previewCandidateQuery" placeholder="名称、摘要或别名" />
              </label>
              <label>
                <span>文档</span>
                <select v-model="previewCandidateDocId">
                  <option value="">全部文档</option>
                  <option
                    v-for="document in previewCandidateDocuments"
                    :key="document.doc_id"
                    :value="document.doc_id"
                  >{{ document.title }}</option>
                </select>
              </label>
              <label>
                <span>来源通道</span>
                <select v-model="previewCandidateChannelId">
                  <option value="">全部通道</option>
                  <option
                    v-for="channel in previewCandidateChannels"
                    :key="channel.channelId"
                    :value="channel.channelId"
                  >{{ channel.name }}</option>
                </select>
              </label>
              <label>
                <span>加入基准时的优先级</span>
                <select v-model="previewCandidatePriority">
                  <option value="must">必须（Must）</option>
                  <option value="should">应该（Should）</option>
                  <option value="nice_to_have">最好有</option>
                  <option value="noise">噪声</option>
                </select>
              </label>
              <div class="candidate-toolbar-actions">
                <span>
                  {{ filteredPreviewCandidates.length }} / {{ activePreviewCandidates.length }}
                  个候选
                </span>
                <button
                  v-if="englishPreviewEvidence.length"
                  type="button"
                  class="translate-action"
                  :disabled="evidenceTranslationBusy || !untranslatedEnglishEvidence.length"
                  @click="requestTranslateEvidence"
                >
                  {{
                    evidenceTranslationBusy
                      ? "翻译中…"
                      : untranslatedEnglishEvidence.length
                        ? `翻译英文证据（${untranslatedEnglishEvidence.length}）`
                        : "英文证据已翻译"
                  }}
                </button>
              </div>
            </div>
            <p
              v-if="evidenceTranslationNotice"
              class="translation-notice"
            >
              {{ evidenceTranslationNotice }}
            </p>

            <div
              v-if="
                activePreview.status === 'completed' &&
                previewResultView === 'candidates' &&
                filteredPreviewCandidateGroups.length
              "
              class="candidate-workspace"
            >
              <aside class="candidate-navigation">
                <article
                  v-for="group in filteredPreviewCandidateGroups"
                  :key="group.document.doc_id"
                  class="candidate-group"
                >
                  <div class="candidate-group-heading">
                    <strong>{{ group.document.title }}</strong>
                    <span>{{ group.candidates.length }}</span>
                  </div>
                  <button
                    v-for="candidate in group.candidates"
                    :key="candidate.candidate_id"
                    type="button"
                    :class="{ active: selectedPreviewCandidate?.candidate_id === candidate.candidate_id }"
                    @click="selectPreviewCandidate(candidate.candidate_id)"
                  >
                    <strong>{{ candidate.name }}</strong>
                    <small>{{ candidate.summary }}</small>
                    <span>{{ candidate.source_block_ids.length }} 个证据块</span>
                  </button>
                </article>
              </aside>

              <article
                v-if="selectedPreviewCandidate"
                class="candidate-inspector"
              >
                <header>
                  <div>
                    <p class="eyebrow">CANDIDATE</p>
                    <h4>{{ selectedPreviewCandidate.name }}</h4>
                    <span>{{ documentTitle(selectedPreviewCandidate.doc_id) }}</span>
                  </div>
                  <button
                    type="button"
                    class="primary-action"
                    :disabled="busy"
                    @click="appendCandidateToChecklist(selectedPreviewCandidate)"
                  >加入质量校验基准</button>
                </header>
                <div class="candidate-meta">
                  <span
                    v-for="channelId in selectedPreviewCandidate.channel_ids"
                    :key="channelId"
                  >{{ channelNameForId(channelId) }}</span>
                  <span>{{ selectedPreviewCandidate.source_block_ids.length }} 个证据块</span>
                </div>
                <section class="candidate-alias-section">
                  <strong>别名</strong>
                  <div v-if="selectedPreviewCandidate.aliases.length" class="candidate-alias-list">
                    <span
                      v-for="alias in selectedPreviewCandidate.aliases"
                      :key="alias"
                    >{{ alias }}</span>
                  </div>
                  <p v-else>无别名</p>
                </section>
                <section class="candidate-summary-section">
                  <p class="eyebrow">模型归纳</p>
                  <p>{{ selectedPreviewCandidate.summary }}</p>
                  <small>这是模型生成的解释，不是原文引用。</small>
                </section>
                <section class="candidate-evidence-section">
                  <div class="candidate-group-heading">
                    <strong>原始证据</strong>
                    <span>{{ selectedPreviewCandidate.evidence_blocks.length }}</span>
                  </div>
                  <div class="candidate-evidence">
                    <details
                      v-for="evidence in selectedPreviewCandidate.evidence_blocks"
                      :key="evidence.block_id"
                    >
                      <summary>{{ evidence.section_path.join(" / ") || evidence.block_id }}</summary>
                      <p>{{ evidence.text }}</p>
                      <div
                        v-if="activePreviewTranslations[evidence.block_id]"
                        class="evidence-translation"
                      >
                        <strong>中文翻译</strong>
                        <p>{{ activePreviewTranslations[evidence.block_id] }}</p>
                        <small>自动翻译，仅用于阅读；事实判断以英文原文为准。</small>
                      </div>
                      <code>{{ evidence.block_id }}</code>
                    </details>
                  </div>
                </section>
              </article>
            </div>

            <section
              v-if="
                activePreview.status === 'completed' &&
                previewResultView === 'source-order'
              "
              class="source-order-view"
            >
              <div class="source-order-toolbar">
                <label>
                  <span>阅读文档</span>
                  <select
                    v-model="sourceOrderDocId"
                    @change="loadSourceOrderDocument(sourceOrderDocId)"
                  >
                    <option value="">选择文档</option>
                    <option
                      v-for="document in activePreviewDocuments"
                      :key="document.doc_id"
                      :value="document.doc_id"
                    >{{ document.title }}</option>
                  </select>
                </label>
                <div class="source-order-stats">
                  <strong>{{ sourceOrderCoveredBlockCount }} / {{ sourceOrderBlocks.length }}</strong>
                  <span>个原文块关联了候选</span>
                </div>
                <div class="source-language-control">
                  <span>显示语言</span>
                  <div class="source-language-switch">
                    <button
                      type="button"
                      :class="{ active: sourceOrderLanguage === 'source' }"
                      :disabled="sourceOrderBusy || evidenceTranslationBusy"
                      @click="setSourceOrderLanguage('source')"
                    >原文</button>
                    <button
                      type="button"
                      :class="{ active: sourceOrderLanguage === 'zh' }"
                      :disabled="sourceOrderBusy || evidenceTranslationBusy"
                      @click="setSourceOrderLanguage('zh')"
                    >
                      {{ evidenceTranslationBusy ? "翻译中…" : "中文" }}
                    </button>
                  </div>
                </div>
                <label class="inline-check source-order-filter">
                  <input v-model="sourceOrderOnlyUncovered" type="checkbox" />
                  <span>只看未关联候选的原文块</span>
                </label>
              </div>

              <div v-if="sourceOrderBusy" class="source-order-loading" role="status">
                <span class="extraction-spinner" aria-hidden="true"></span>
                正在按原文顺序载入文档 blocks…
              </div>
              <div v-else-if="sourceOrderVisibleBlocks.length" class="source-comparison-list">
                <template
                  v-for="(block, index) in sourceOrderVisibleBlocks"
                  :key="block.block_id"
                >
                  <div v-if="sourceOrderSectionLabel(index)" class="source-section-band">
                    {{ sourceOrderSectionLabel(index) }}
                  </div>
                  <article
                    class="source-comparison-row"
                    :class="{
                      uncovered: !candidatesForSourceBlock(block.block_id).length,
                    }"
                  >
                    <div class="source-text-block">
                      <div class="source-block-heading">
                        <span>原文块 {{ index + 1 }}</span>
                        <code>{{ block.block_id }}</code>
                      </div>
                      <p>
                        {{
                          sourceOrderLanguage === "zh" &&
                          activePreviewTranslations[block.block_id]
                            ? activePreviewTranslations[block.block_id]
                            : block.text
                        }}
                      </p>
                      <small
                        v-if="
                          sourceOrderLanguage === 'zh' &&
                          requiresChineseTranslation(block.text) &&
                          !activePreviewTranslations[block.block_id]
                        "
                        class="source-translation-missing"
                      >
                        中文翻译尚未生成，暂显示原文。
                      </small>
                      <details
                        v-if="
                          sourceOrderLanguage === 'zh' &&
                          activePreviewTranslations[block.block_id]
                        "
                        class="source-block-translation"
                      >
                        <summary>查看原文</summary>
                        <p>{{ block.text }}</p>
                      </details>
                    </div>
                    <div class="source-linked-candidates">
                      <template v-if="candidatesForSourceBlock(block.block_id).length">
                        <article
                          v-for="candidate in candidatesForSourceBlock(block.block_id)"
                          :key="candidate.candidate_id"
                          class="source-linked-candidate"
                          :class="{
                            'is-compact': !isFirstSourceCandidateOccurrence(
                              candidate.candidate_id,
                              block.block_id,
                            ),
                          }"
                        >
                          <header>
                            <div>
                              <strong>{{ candidate.name }}</strong>
                              <small>
                                <template
                                  v-if="
                                    isFirstSourceCandidateOccurrence(
                                      candidate.candidate_id,
                                      block.block_id,
                                    )
                                  "
                                >
                                  {{
                                    candidate.channel_ids
                                      .map(channelNameForId)
                                      .join(" · ")
                                  }}
                                </template>
                                <template v-else>也引用了本段</template>
                              </small>
                            </div>
                            <button
                              type="button"
                              @click="openSourceCandidateDetail(candidate.candidate_id)"
                            >查看详情</button>
                          </header>
                          <template
                            v-if="
                              isFirstSourceCandidateOccurrence(
                                candidate.candidate_id,
                                block.block_id,
                              )
                            "
                          >
                            <p>{{ candidate.summary }}</p>
                            <footer>
                              <span>{{ candidate.source_block_ids.length }} 个证据块</span>
                              <button
                                type="button"
                                :disabled="busy"
                                @click="appendCandidateToChecklist(candidate)"
                              >加入质量校验基准</button>
                            </footer>
                          </template>
                        </article>
                      </template>
                      <div v-else class="uncovered-block-note">
                        <strong>未关联候选</strong>
                        <p>这不等同于漏提，需要结合原文判断这里是否应产生知识条目。</p>
                      </div>
                    </div>
                  </article>
                </template>
              </div>
              <div v-else class="empty-state">
                {{
                  sourceOrderOnlyUncovered
                    ? "这篇文档的当前结果没有未关联候选的原文块。"
                    : "选择一篇文档后查看按原文顺序组织的候选。"
                }}
              </div>
            </section>

            <p
              v-if="
                activePreview.status === 'completed' &&
                previewResultView === 'candidates' &&
                !activePreviewCandidates.length
              "
              class="empty-state"
            >
              本次试抽取没有产生候选。可以检查 Profile 条件或更换样本文档后重试。
            </p>
            <p
              v-else-if="
                activePreview.status === 'completed' &&
                previewResultView === 'candidates' &&
                !filteredPreviewCandidateGroups.length
              "
              class="empty-state"
            >
              没有符合当前筛选条件的候选。
            </p>
          </template>
          <div v-else class="empty-state">选择一条试抽取记录查看候选。</div>
        </div>
      </div>
    </template>

    <template v-if="activeTab === 'checklists'">
      <section class="tab-intro">
        <strong>为文档定义 Profile 比较的质量校验标准</strong>
        <p>
          这里不是在查看某次抽取得到的候选，而是提前定义这篇文档中哪些知识对象
          必须、应该或最好被抽出。冻结后，这份基准用于判断两个 Profile 的抽取结果。
        </p>
      </section>
      <div class="checklist-create-row">
        <label>
          <span>文档</span>
          <select v-model="checklistDocId" @change="loadChecklistBlocks(checklistDocId)">
            <option value="">选择文档</option>
            <option
              v-for="document in documents"
              :key="document.doc_id"
              :value="document.doc_id"
            >{{ document.title }}</option>
          </select>
        </label>
        <label class="grow">
          <span>校验说明（可选）</span>
          <input v-model="checklistNote" placeholder="例如：用于验证学习类问答的覆盖范围" />
        </label>
        <button type="button" :disabled="busy || !checklistDocId" @click="createEmptyChecklist">
          新建空白基准
        </button>
        <button
          type="button"
          class="primary-action"
          :disabled="busy || !checklistDocId"
          @click="suggestChecklistItems"
        >
          生成校验项建议
        </button>
      </div>

      <div class="preview-layout">
        <aside class="preview-run-list">
          <div class="aside-heading">
            <strong>已有校验基准</strong>
            <span>{{ checklists.length }} 份</span>
          </div>
          <p v-if="!checklists.length" class="aside-empty">
            还没有质量校验基准。先选择文档，再从系统建议开始或创建空白基准。
          </p>
          <button
            v-for="checklist in checklists"
            :key="checklist.checklist_id"
            type="button"
            :class="{ active: activeChecklist?.checklist_id === checklist.checklist_id }"
            @click="openChecklist(checklist)"
          >
            <strong>{{ documentTitle(checklist.doc_id) }}</strong>
            <span>{{ checklistStatusLabel(checklist.status) }} · {{ checklist.items.length }} 项</span>
            <small>{{ checklist.checklist_hash.slice(0, 12) }}</small>
          </button>
        </aside>
        <div class="preview-detail">
          <template v-if="activeChecklist">
            <div class="channel-card-heading">
              <div>
                <strong>{{ documentTitle(activeChecklist.doc_id) }}</strong>
                <small>
                  {{ checklistStatusLabel(activeChecklist.status) }} ·
                  {{ activeChecklist.checklist_id }}
                </small>
              </div>
              <span class="status-pill">{{ checklistStatusLabel(activeChecklist.status) }}</span>
            </div>
            <label class="block-field">
              <span>评测说明</span>
              <input v-model="checklistNote" :disabled="activeChecklist.status === 'frozen'" />
            </label>
            <p v-if="!checklistItems.length" class="empty-state">
              还没有校验项。可以生成建议、从试抽取候选加入，或手工添加。
            </p>
            <div
              v-for="item in checklistItems"
              :key="item.item_id"
              class="checklist-item-block"
            >
              <article class="checklist-item-row">
                <input v-model="item.name" :disabled="activeChecklist.status === 'frozen'" />
                <select v-model="item.priority" :disabled="activeChecklist.status === 'frozen'">
                  <option value="must">必须（Must）</option>
                  <option value="should">应该（Should）</option>
                  <option value="nice_to_have">最好有</option>
                  <option value="noise">噪声</option>
                </select>
                <span>{{ item.source_block_ids.length }} 个证据块</span>
                <button
                  v-if="activeChecklist.status === 'draft'"
                  type="button"
                  class="danger-link"
                  @click="checklistItems = checklistItems.filter((value) => value.item_id !== item.item_id)"
                >删除</button>
              </article>
              <details class="checklist-evidence">
                <summary>
                  查看原文证据
                  <span>{{ checklistPriorityLabel(item.priority) }}</span>
                </summary>
                <ul>
                  <li
                    v-for="block in checklistEvidenceBlocks(item.source_block_ids)"
                    :key="block.block_id"
                  >
                    <strong>{{ block.section_path.join(" / ") || block.block_id }}</strong>
                    <p>{{ block.text }}</p>
                  </li>
                </ul>
              </details>
            </div>
            <div v-if="activeChecklist.status === 'draft'" class="manual-item-row">
              <input v-model="manualItemName" placeholder="手工添加知识对象" />
              <select v-model="manualItemPriority">
                <option value="must">必须（Must）</option>
                <option value="should">应该（Should）</option>
                <option value="nice_to_have">最好有</option>
                <option value="noise">噪声</option>
              </select>
              <select v-model="manualItemBlockId">
                <option
                  v-for="block in checklistBlocks"
                  :key="block.block_id"
                  :value="block.block_id"
                >{{ block.section_path.join(" / ") }} · {{ block.text.slice(0, 48) }}</option>
              </select>
              <button type="button" @click="addManualChecklistItem">添加</button>
            </div>
            <div class="profile-actions">
              <button
                v-if="activeChecklist.status === 'draft'"
                type="button"
                :disabled="busy"
                @click="saveActiveChecklist"
              >保存草稿</button>
              <button
                v-if="activeChecklist.status === 'draft'"
                type="button"
                class="primary-action"
                :disabled="busy"
                @click="requestFreezeActiveChecklist"
              >冻结基准</button>
              <button
                v-if="activeChecklist.status === 'draft'"
                type="button"
                class="danger-link"
                :disabled="busy"
                @click="requestDeleteActiveChecklist"
              >删除草稿</button>
            </div>
          </template>
          <div v-else class="empty-state">选择或创建一份文档质量校验基准。</div>
        </div>
      </div>
    </template>

    <template v-if="activeTab === 'comparisons'">
      <section class="tab-intro">
        <strong>用同一份质量校验基准，比较两次试抽取</strong>
        <p>
          选择两个已完成的试抽取结果和至少一份冻结基准。系统逐文档展示覆盖情况，
          不生成没有依据的综合分数，最终结论由你确认。
        </p>
      </section>
      <div class="comparison-builder">
        <label>
          <span>Profile A 的试抽取结果</span>
          <select v-model="comparisonRunAId">
            <option value="">选择结果 A</option>
            <option v-for="run in completedRuns" :key="run.run_id" :value="run.run_id">
              {{ profileNameForRun(run) }} · {{ new Date(run.created_at).toLocaleString() }}
            </option>
          </select>
        </label>
        <label>
          <span>Profile B 的试抽取结果</span>
          <select v-model="comparisonRunBId">
            <option value="">选择结果 B</option>
            <option v-for="run in completedRuns" :key="run.run_id" :value="run.run_id">
              {{ profileNameForRun(run) }} · {{ new Date(run.created_at).toLocaleString() }}
            </option>
          </select>
        </label>
        <div>
          <span class="field-label">冻结质量校验基准</span>
          <label v-for="checklist in frozenChecklists" :key="checklist.checklist_id" class="document-check">
            <input
              type="checkbox"
              :checked="selectedChecklistIds.includes(checklist.checklist_id)"
              @change="toggleComparisonChecklist(checklist.checklist_id)"
            />
            <span>{{ documentTitle(checklist.doc_id) }} · {{ checklist.items.length }} 项</span>
          </label>
          <small v-if="!frozenChecklists.length" class="field-note">
            还没有冻结的质量校验基准。请先到“质量校验基准”页完成创建和冻结。
          </small>
        </div>
        <small v-if="!completedRuns.length" class="field-note">
          还没有已完成的试抽取结果。请先到“试抽取”页完成两次试跑。
        </small>
        <button
          type="button"
          class="primary-action"
          :disabled="
            busy ||
            !comparisonRunAId ||
            !comparisonRunBId ||
            comparisonRunAId === comparisonRunBId ||
            !selectedChecklistIds.length
          "
          @click="createComparison"
        >
          生成比较
        </button>
      </div>

      <div v-if="activeComparison" class="comparison-result">
        <div class="channel-card-heading">
          <strong>比较结果</strong>
          <span class="status-pill">{{ comparisonStatusLabel(activeComparison.status) }}</span>
        </div>
        <article
          v-for="document in activeComparison.document_results"
          :key="document.doc_id"
          class="comparison-document"
        >
          <h4>{{ documentTitle(document.doc_id) }}</h4>
          <div class="comparison-count-grid">
            <div>
              <strong>A · {{ comparisonRunA ? profileNameForRun(comparisonRunA) : "Profile A" }}</strong>
              <span>必须项命中：{{ document.profile_a.must.matched }}/{{ document.profile_a.must.matched + document.profile_a.must.missing + document.profile_a.must.uncertain }}</span>
              <span>应该项缺失：{{ document.profile_a.should.missing }}</span>
              <span>噪声命中：{{ document.profile_a.noise_hits }}</span>
            </div>
            <div>
              <strong>B · {{ comparisonRunB ? profileNameForRun(comparisonRunB) : "Profile B" }}</strong>
              <span>必须项命中：{{ document.profile_b.must.matched }}/{{ document.profile_b.must.matched + document.profile_b.must.missing + document.profile_b.must.uncertain }}</span>
              <span>应该项缺失：{{ document.profile_b.should.missing }}</span>
              <span>噪声命中：{{ document.profile_b.noise_hits }}</span>
            </div>
          </div>
          <article
            v-for="item in document.item_results"
            :key="item.item_id"
            class="comparison-item"
          >
            <strong>{{ item.name }}</strong>
            <span class="priority-chip">{{ checklistPriorityLabel(item.priority) }}</span>
            <span>A：{{ comparisonItemStatusLabel(item.profile_a.status) }}</span>
            <span>B：{{ comparisonItemStatusLabel(item.profile_b.status) }}</span>
            <template
              v-for="side in (['a', 'b'] as const)"
              :key="`${item.item_id}-${side}`"
            >
              <template
                v-if="
                  (side === 'a' ? item.profile_a : item.profile_b).status ===
                    'uncertain_match' && activeComparison.status === 'draft'
                "
              >
                <select v-model="uncertainCandidateChoice[`${item.item_id}:${side}`]">
                  <option value="">选择匹配的候选项</option>
                  <option
                    v-for="candidate in sideCandidates(
                      document,
                      side,
                      side === 'a'
                        ? item.profile_a.candidate_ids
                        : item.profile_b.candidate_ids,
                    )"
                    :key="candidate.candidate_id"
                    :value="candidate.candidate_id"
                  >{{ candidate.name }}</option>
                </select>
                <button
                  type="button"
                  :disabled="busy"
                  @click="resolveUncertain(item.item_id, side, 'matched')"
                >确认命中</button>
                <button
                  type="button"
                  :disabled="busy"
                  @click="resolveUncertain(item.item_id, side, 'missing')"
                >确认缺失</button>
              </template>
            </template>
          </article>
          <label v-if="activeComparison.status === 'draft'" class="block-field">
            <span>本文件结论</span>
            <select v-model="documentOutcomes[document.doc_id]">
              <option value="a_better">Profile A 更好</option>
              <option value="b_better">Profile B 更好</option>
              <option value="equivalent">两者基本相当</option>
              <option value="both_insufficient">两者都不够好</option>
            </select>
          </label>
          <label
            v-if="
              activeComparison.status === 'draft' &&
              (document.profile_a.must.missing > 0 ||
                document.profile_b.must.missing > 0 ||
                document.profile_a.must.uncertain > 0 ||
                document.profile_b.must.uncertain > 0)
            "
            class="block-field"
          >
            <span>忽略 must 阻断的原因</span>
            <input v-model="mustOverrideReasons[document.doc_id]" />
          </label>
        </article>

        <div v-if="activeComparison.status === 'draft'" class="comparison-conclusion">
          <label>
            <span>总体结论</span>
            <select v-model="comparisonOutcome">
              <option value="a_better">Profile A 更好</option>
              <option value="b_better">Profile B 更好</option>
              <option value="equivalent">两者基本相当</option>
              <option value="both_insufficient">两者都不够好</option>
            </select>
          </label>
          <label class="grow">
            <span>结论说明</span>
            <textarea v-model="comparisonNote" />
          </label>
          <button
            type="button"
            class="primary-action"
            :disabled="busy"
            @click="requestConcludeComparison"
          >
            提交并冻结结论
          </button>
        </div>
      </div>
      <div v-else class="empty-state comparison-empty">
        选择两个已完成试抽取结果和冻结的质量校验基准，然后生成比较。
      </div>

      <div class="history-section">
        <h3>比较历史</h3>
        <p v-if="!comparisons.length" class="empty-state">还没有保存过比较结论。</p>
        <button
          v-for="comparison in comparisons"
          :key="comparison.comparison_id"
          type="button"
          class="history-row"
          @click="loadComparison(comparison)"
        >
          <span>
            {{ comparison.outcome ? comparisonOutcomeLabel(comparison.outcome) : "尚未得出结论" }}
          </span>
          <small>
            {{ comparison.comparison_id }} ·
            {{ new Date(comparison.created_at).toLocaleString() }}
          </small>
        </button>
      </div>
    </template>

    <div
      v-if="sourceCandidateDetail"
      class="dialog-backdrop source-candidate-backdrop"
      @click.self="closeSourceCandidateDetail"
    >
      <section
        class="source-candidate-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="source-candidate-title"
      >
        <header class="source-candidate-dialog-heading">
          <div>
            <p class="eyebrow">CANDIDATE IN SOURCE CONTEXT</p>
            <h3 id="source-candidate-title">{{ sourceCandidateDetail.name }}</h3>
            <span>{{ documentTitle(sourceCandidateDetail.doc_id) }}</span>
          </div>
          <button type="button" @click="closeSourceCandidateDetail">关闭</button>
        </header>
        <div class="candidate-meta">
          <span
            v-for="channelId in sourceCandidateDetail.channel_ids"
            :key="channelId"
          >{{ channelNameForId(channelId) }}</span>
          <span>{{ sourceCandidateDetail.source_block_ids.length }} 个证据块</span>
        </div>
        <section class="candidate-alias-section">
          <strong>别名</strong>
          <div v-if="sourceCandidateDetail.aliases.length" class="candidate-alias-list">
            <span
              v-for="alias in sourceCandidateDetail.aliases"
              :key="alias"
            >{{ alias }}</span>
          </div>
          <p v-else>无别名</p>
        </section>
        <section class="candidate-summary-section">
          <p class="eyebrow">模型归纳</p>
          <p>{{ sourceCandidateDetail.summary }}</p>
          <small>这是模型生成的解释，不是原文引用。</small>
        </section>
        <section class="candidate-evidence-section">
          <div class="candidate-group-heading">
            <strong>原始证据</strong>
            <span>{{ sourceCandidateDetail.evidence_blocks.length }}</span>
          </div>
          <div class="candidate-evidence">
            <details
              v-for="evidence in sourceCandidateDetail.evidence_blocks"
              :key="evidence.block_id"
            >
              <summary>{{ evidence.section_path.join(" / ") || evidence.block_id }}</summary>
              <p>{{ evidence.text }}</p>
              <div
                v-if="activePreviewTranslations[evidence.block_id]"
                class="evidence-translation"
              >
                <strong>中文翻译</strong>
                <p>{{ activePreviewTranslations[evidence.block_id] }}</p>
                <small>自动翻译，仅用于阅读；事实判断以英文原文为准。</small>
              </div>
              <code>{{ evidence.block_id }}</code>
            </details>
          </div>
        </section>
        <footer class="source-candidate-dialog-actions">
          <button type="button" @click="closeSourceCandidateDetail">返回原文阅读</button>
          <button
            type="button"
            class="primary-action"
            :disabled="busy"
            @click="addSourceCandidateToChecklist(sourceCandidateDetail)"
          >加入质量校验基准</button>
        </footer>
      </section>
    </div>

    <div
      v-if="showSelectionDialog && pendingSelectionProfile"
      class="dialog-backdrop"
      @click.self="showSelectionDialog = false"
    >
      <section
        class="selection-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="profile-selection-title"
      >
        <p class="eyebrow">CONFIRM PROFILE</p>
        <h3 id="profile-selection-title">
          确定使用“{{ pendingSelectionProfile.config.name }}”？
        </h3>
        <p>
          系统检查 Profile 是否完整，以及通道和排除规则是否存在直接冲突。检查通过后冻结
          并设为当前 Profile，只更新当前选择，不执行试抽取或全库构建。冻结后该
          Profile 不可更改。
        </p>
        <p class="dialog-warning">
          该 Profile 将冻结且不可编辑；你仍可切换其他 Profile，或复制它创建新的草稿。
          此操作不会执行试抽取或全库构建。
        </p>
        <p v-if="!hasCompletedPreview(pendingSelectionProfile)" class="dialog-warning">
          该 Profile 尚未在样本文档上成功试抽取。
        </p>
        <div class="dialog-meta">
          <span>{{ pendingSelectionProfile.profile_id }}</span>
          <span>{{ pendingSelectionProfile.profile_hash.slice(0, 16) }}</span>
        </div>
        <div class="dialog-actions">
          <button type="button" @click="showSelectionDialog = false">取消</button>
          <button
            type="button"
            class="primary-action"
            :disabled="busy"
            @click="executeProfileSelection"
          >确认并设为当前 Profile</button>
        </div>
      </section>
    </div>

    <div
      v-if="pendingConfirmation"
      class="dialog-backdrop confirmation-backdrop"
      @click.self="closeConfirmation"
    >
      <section
        class="selection-dialog confirmation-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirmation-dialog-title"
      >
        <p class="eyebrow">CONFIRM ACTION</p>
        <h3 id="confirmation-dialog-title">{{ pendingConfirmation.title }}</h3>
        <p>{{ pendingConfirmation.message }}</p>
        <ul v-if="pendingConfirmation.details?.length" class="dialog-detail-list">
          <li v-for="detail in pendingConfirmation.details" :key="detail">{{ detail }}</li>
        </ul>
        <div class="dialog-actions">
          <button type="button" :disabled="confirmationBusy" @click="closeConfirmation">
            取消
          </button>
          <button
            type="button"
            :class="pendingConfirmation.isDangerous ? 'danger-action' : 'primary-action'"
            :disabled="confirmationBusy"
            @click="executeConfirmation"
          >
            {{ confirmationBusy ? "处理中…" : pendingConfirmation.confirmLabel }}
          </button>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.profile-workbench {
  display: grid;
  gap: 20px;
  margin-top: 22px;
}

.workbench-subtitle {
  max-width: 680px;
  margin: 7px 0 0;
  color: #66776c;
  font-size: 0.86rem;
  line-height: 1.6;
}

.profile-workbench button {
  min-height: 36px;
  border: 1px solid rgba(37, 71, 50, 0.2);
  border-radius: 10px;
  padding: 8px 13px;
  color: #254d34;
  background: linear-gradient(180deg, #fbfdf9 0%, #edf4eb 100%);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.75) inset;
  cursor: pointer;
  font-size: 0.82rem;
  font-weight: 650;
  transition:
    border-color 150ms ease,
    background-color 150ms ease,
    color 150ms ease,
    transform 150ms ease,
    box-shadow 150ms ease;
}

.profile-workbench button:hover:not(:disabled) {
  border-color: rgba(37, 104, 65, 0.52);
  background: #e5f0e5;
  box-shadow: 0 5px 14px rgba(37, 76, 50, 0.1);
  transform: translateY(-1px);
}

.profile-workbench button:active:not(:disabled) {
  box-shadow: none;
  transform: translateY(0);
}

.profile-workbench button:focus-visible,
.profile-workbench input:focus-visible,
.profile-workbench textarea:focus-visible,
.profile-workbench select:focus-visible,
.profile-workbench summary:focus-visible {
  outline: 3px solid rgba(38, 119, 72, 0.24);
  outline-offset: 2px;
}

.profile-workbench button:disabled {
  cursor: not-allowed;
  filter: saturate(0.35);
  opacity: 0.48;
  transform: none;
}

.profile-workbench button.primary-action {
  border-color: #255f3c;
  color: #fff;
  background: linear-gradient(180deg, #2c7047 0%, #214e32 100%);
  box-shadow: 0 8px 18px rgba(32, 80, 49, 0.2);
}

.profile-workbench button.primary-action:hover:not(:disabled) {
  border-color: #1d4f31;
  background: linear-gradient(180deg, #327c50 0%, #245839 100%);
  box-shadow: 0 10px 22px rgba(32, 80, 49, 0.25);
}

.profile-workbench button.danger-link {
  border-color: rgba(171, 68, 68, 0.28);
  color: #913737;
  background: #fff7f6;
  box-shadow: none;
}

.profile-workbench button.danger-link:hover:not(:disabled) {
  border-color: rgba(171, 68, 68, 0.52);
  background: #fdeceb;
  box-shadow: 0 5px 14px rgba(128, 42, 42, 0.1);
}

.profile-workbench button.danger-action {
  border-color: #8c3535;
  color: #fff;
  background: linear-gradient(180deg, #a94343 0%, #833333 100%);
  box-shadow: 0 8px 18px rgba(128, 42, 42, 0.2);
}

.profile-workbench button.danger-action:hover:not(:disabled) {
  border-color: #702828;
  background: linear-gradient(180deg, #b64c4c 0%, #913838 100%);
}

.profile-workbench input:not([type="checkbox"]),
.profile-workbench textarea,
.profile-workbench select {
  border-color: rgba(37, 71, 50, 0.22);
  color: #1f3627;
  background: rgba(255, 255, 255, 0.94);
  font: inherit;
  transition:
    border-color 150ms ease,
    box-shadow 150ms ease,
    background-color 150ms ease;
}

.profile-workbench input:not([type="checkbox"]):hover,
.profile-workbench textarea:hover,
.profile-workbench select:hover {
  border-color: rgba(37, 104, 65, 0.42);
}

.profile-workbench input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: #2b7048;
}

.profile-workbench input:disabled,
.profile-workbench textarea:disabled,
.profile-workbench select:disabled {
  cursor: not-allowed;
  color: #68786d;
  background: #edf1eb;
}

.profile-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  padding: 6px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.62);
}

.profile-tabs button {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  border: 0;
  border-radius: 10px;
  padding: 11px 12px;
  background: transparent;
  color: #52665a;
  box-shadow: none;
  text-align: left;
  transition:
    background-color 150ms ease,
    color 150ms ease,
    box-shadow 150ms ease;
}

.profile-tabs button.active,
.profile-tabs button.active:hover {
  background: #214b34;
  color: #fff;
  box-shadow: 0 7px 16px rgba(31, 74, 47, 0.2);
}

.profile-tab-step {
  display: inline-grid;
  flex: 0 0 auto;
  width: 25px;
  height: 25px;
  place-items: center;
  border: 1px solid rgba(37, 104, 65, 0.22);
  border-radius: 8px;
  color: #286b43;
  background: #e8f2e8;
  font-size: 0.72rem;
  font-weight: 800;
}

.profile-tab-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.profile-tab-copy strong,
.profile-tab-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-tab-copy strong {
  font-size: 0.82rem;
}

.profile-tab-copy small {
  color: #7b8a80;
  font-size: 0.68rem;
  font-weight: 500;
}

.profile-tabs button.active .profile-tab-step {
  border-color: rgba(255, 255, 255, 0.34);
  color: #214b34;
  background: rgba(255, 255, 255, 0.88);
}

.profile-tabs button.active .profile-tab-copy small {
  color: rgba(255, 255, 255, 0.72);
}

.profile-status-row {
  display: flex;
  align-items: stretch;
  gap: 10px;
}

.active-profile-banner,
.active-profile-warning {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  border-radius: 13px;
  padding: 12px 15px;
}

.active-profile-banner {
  border: 1px solid rgba(35, 106, 63, 0.32);
  color: #275a3a;
  background: linear-gradient(135deg, #e4f2e7 0%, #f5faf3 100%);
}

.active-profile-banner > div {
  display: flex;
  align-items: center;
  gap: 10px;
}

.active-profile-banner > span {
  color: #66786b;
  font-family: Consolas, "Courier New", monospace;
  font-size: 0.72rem;
}

.active-profile-mark {
  border-radius: 999px;
  padding: 4px 8px;
  color: #fff;
  background: #286b43;
  font-size: 0.7rem;
  font-weight: 750;
  letter-spacing: 0.03em;
}

.active-profile-warning {
  justify-content: flex-start;
  border: 1px solid rgba(184, 132, 38, 0.3);
  color: #795b1e;
  background: #fff8e8;
  font-weight: 650;
}

.profile-actions,
.channel-editor-heading,
.channel-card-heading,
.profile-card-heading,
.candidate-toolbar,
.checklist-create-row,
.manual-item-row,
.comparison-builder,
.comparison-conclusion {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.profile-create-action {
  flex: 0 0 auto;
  min-width: 128px;
  align-self: stretch;
  white-space: nowrap;
}

.profile-actions {
  margin-top: 12px;
}

.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: 14px;
}

.profile-card,
.profile-editor,
.preview-builder,
.preview-detail,
.preview-run-list,
.comparison-builder,
.comparison-document,
.comparison-conclusion,
.history-section {
  padding: 20px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.72);
}

.profile-card {
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    transform 160ms ease;
}

.profile-card:hover {
  border-color: rgba(37, 104, 65, 0.3);
  box-shadow: 0 12px 28px rgba(45, 69, 52, 0.1);
  transform: translateY(-1px);
}

.profile-card.is-viewing {
  border-color: rgba(37, 104, 65, 0.5);
  box-shadow: 0 12px 30px rgba(36, 87, 53, 0.14);
}

.profile-card.is-selected {
  border: 2px solid #2e754b;
  background:
    linear-gradient(135deg, rgba(224, 242, 228, 0.94), rgba(255, 255, 255, 0.88));
  box-shadow: 0 14px 34px rgba(34, 91, 53, 0.16);
}

.profile-card.is-selected::before {
  display: block;
  width: 42px;
  height: 4px;
  margin: -4px 0 13px;
  border-radius: 999px;
  background: #2e754b;
  content: "";
}

.selection-action {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding-right: 16px !important;
}

.button-info {
  position: absolute !important;
  top: 3px;
  right: 4px;
  width: 16px !important;
  height: 16px !important;
  border-color: rgba(255, 255, 255, 0.52) !important;
  color: inherit !important;
  background: rgba(255, 255, 255, 0.18) !important;
  font-size: 0.62rem !important;
}

.info-tooltip {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 19px;
  height: 19px;
  border: 1px solid rgba(37, 104, 65, 0.42);
  border-radius: 50%;
  color: #286b43;
  background: rgba(238, 247, 239, 0.96);
  cursor: help;
  font-size: 0.72rem;
  font-style: normal;
  font-weight: 800;
  line-height: 1;
}

.info-tooltip > [role="tooltip"] {
  position: absolute;
  z-index: 50;
  left: 50%;
  bottom: calc(100% + 9px);
  width: 285px;
  border: 1px solid rgba(30, 63, 41, 0.22);
  border-radius: 10px;
  padding: 10px 12px;
  color: #edf7ef;
  background: #203d2a;
  box-shadow: 0 12px 30px rgba(24, 52, 33, 0.24);
  font-size: 0.76rem;
  font-style: normal;
  font-weight: 500;
  line-height: 1.55;
  opacity: 0;
  pointer-events: none;
  text-align: left;
  transform: translate(-50%, 4px);
  transition:
    opacity 140ms ease,
    transform 140ms ease,
    visibility 140ms ease;
  visibility: hidden;
}

.info-tooltip > [role="tooltip"]::after {
  position: absolute;
  top: 100%;
  left: 50%;
  width: 8px;
  height: 8px;
  background: #203d2a;
  content: "";
  transform: translate(-50%, -4px) rotate(45deg);
}

.info-tooltip:hover > [role="tooltip"],
.info-tooltip:focus > [role="tooltip"],
.info-tooltip:focus-within > [role="tooltip"] {
  opacity: 1;
  transform: translate(-50%, 0);
  visibility: visible;
}

.dialog-backdrop {
  position: fixed;
  z-index: 100;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(24, 43, 30, 0.35);
  backdrop-filter: blur(5px);
}

.profile-detail-backdrop {
  z-index: 90;
}

.profile-detail-dialog {
  width: min(1040px, calc(100vw - 48px));
  max-height: calc(100vh - 48px);
  overflow: auto;
  background: #f8fbf7;
  box-shadow: 0 28px 80px rgba(18, 44, 27, 0.3);
  animation: profile-editor-reveal 220ms ease-out;
}

.profile-detail-dialog > .section-heading {
  position: sticky;
  z-index: 20;
  top: -20px;
  margin: -20px -20px 14px;
  padding: 18px 20px 14px;
  border-bottom: 1px solid rgba(37, 71, 50, 0.12);
  background: #f8fbf7;
}

.profile-editor-heading-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

@keyframes profile-editor-reveal {
  from {
    opacity: 0;
    transform: translateY(10px) scale(0.99);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.selection-dialog {
  width: min(560px, 100%);
  border: 1px solid rgba(37, 71, 50, 0.2);
  border-radius: 20px;
  padding: 26px;
  background: #f8fbf7;
  box-shadow: 0 26px 70px rgba(20, 47, 30, 0.25);
}

.selection-dialog h3 {
  margin: 8px 0 14px;
  color: #1d3d28;
  font-family: Georgia, "Songti SC", serif;
  font-size: 1.45rem;
}

.selection-dialog > p {
  color: #506258;
  line-height: 1.7;
}

.confirmation-backdrop {
  z-index: 120;
}

.source-candidate-backdrop {
  z-index: 110;
}

.source-candidate-dialog {
  width: min(780px, 100%);
  max-height: calc(100vh - 48px);
  overflow: auto;
  border: 1px solid rgba(37, 71, 50, 0.18);
  border-radius: 18px;
  padding: 22px;
  background: #f8fbf7;
  box-shadow: 0 26px 70px rgba(20, 47, 30, 0.26);
}

.source-candidate-dialog-heading {
  position: sticky;
  z-index: 5;
  top: -22px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin: -22px -22px 0;
  padding: 20px 22px 14px;
  border-bottom: 1px solid rgba(37, 71, 50, 0.12);
  background: rgba(248, 251, 247, 0.97);
  backdrop-filter: blur(12px);
}

.source-candidate-dialog-heading h3 {
  margin: 5px 0 3px;
  color: #203f2b;
  font-size: 1.35rem;
}

.source-candidate-dialog-heading span {
  color: #728177;
  font-size: 0.75rem;
}

.source-candidate-dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid rgba(37, 71, 50, 0.12);
}

.confirmation-dialog {
  max-width: 540px;
}

.dialog-detail-list {
  display: grid;
  gap: 6px;
  margin: 14px 0 0;
  padding: 12px 14px 12px 30px;
  border-radius: 10px;
  color: #4d6255;
  background: #edf4eb;
  font-size: 0.8rem;
  line-height: 1.5;
}

.dialog-warning {
  border-left: 3px solid #bf8e2b;
  padding: 8px 11px;
  background: #fff8e8;
}

.dialog-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 14px;
  color: #6d7d72;
  font-family: Consolas, "Courier New", monospace;
  font-size: 0.72rem;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  margin-top: 20px;
}

.profile-card-heading,
.channel-editor-heading,
.channel-card-heading,
.candidate-toolbar {
  justify-content: space-between;
}

.profile-card-heading small,
.profile-meta,
.channel-card-heading small,
.candidate-card small {
  display: block;
  margin-top: 4px;
  color: #708075;
}

.profile-signals {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.profile-signal {
  border-radius: 999px;
  padding: 4px 8px;
  color: #6c7b71;
  background: rgba(232, 238, 230, 0.86);
  font-size: 0.68rem;
  font-weight: 650;
}

.profile-signal.verified {
  color: #23613d;
  background: #dceee1;
}

.section-count {
  color: #718177;
  font-size: 0.72rem;
  font-weight: 600;
}

.selected-badge,
.status-pill {
  padding: 5px 9px;
  border-radius: 999px;
  background: #d8ebdf;
  color: #205d39;
  font-size: 0.75rem;
  font-weight: 700;
}

.error-inline {
  color: #9d3030;
  font-weight: 700;
}

.profile-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.profile-form-grid label,
.preview-builder label,
.comparison-builder label,
.checklist-create-row label,
.block-field {
  display: grid;
  gap: 6px;
  color: #53665a;
  font-size: 0.82rem;
}

.label-with-help {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.label-with-help > span:first-child {
  min-width: 0;
}

.profile-form-grid .span-two,
.grow {
  grid-column: 1 / -1;
  flex: 1;
}

.profile-form-grid input,
.profile-form-grid textarea,
.profile-form-grid select,
.preview-builder input,
.preview-builder select,
.comparison-builder select,
.checklist-create-row input,
.checklist-create-row select,
.manual-item-row input,
.manual-item-row select,
.checklist-item-row input,
.checklist-item-row select,
.comparison-item select,
.comparison-conclusion select,
.comparison-conclusion textarea,
.block-field input {
  width: 100%;
  padding: 9px 10px;
  border: 1px solid rgba(37, 71, 50, 0.2);
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.88);
}

.profile-form-grid textarea,
.comparison-conclusion textarea {
  min-height: 72px;
  resize: vertical;
}

.channel-card,
.suggestion-panel,
.validation-list,
.preview-plan,
.candidate-card,
.comparison-item {
  margin-top: 14px;
  padding: 16px;
  border: 1px solid rgba(37, 71, 50, 0.12);
  border-radius: 13px;
  background: rgba(244, 248, 242, 0.74);
}

.strategy-generator {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  align-items: center;
  margin-top: 16px;
  padding: 15px 16px;
  border: 1px solid rgba(37, 104, 65, 0.18);
  border-radius: 13px;
  background: linear-gradient(135deg, #f3f9f1 0%, #edf5eb 100%);
}

.strategy-generator-title {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #244f34;
}

.strategy-generator-copy p {
  margin: 5px 0 0;
  color: #65766b;
  font-size: 0.8rem;
  line-height: 1.6;
}

.strategy-generator-action {
  display: grid;
  justify-items: end;
  gap: 5px;
}

.strategy-generator-action small {
  color: #8a6a2b;
  font-size: 0.72rem;
}

.strategy-suggestion-panel {
  border-color: rgba(37, 104, 65, 0.24);
  background: #f8fcf7;
}

.strategy-suggestion-panel ol {
  margin: 12px 0 0;
  padding-left: 22px;
  color: #40554a;
  line-height: 1.7;
}

.strategy-suggestion-note {
  margin: 12px 0 0;
  border-left: 3px solid #bf8e2b;
  padding: 7px 10px;
  color: #735824;
  background: #fff8e8;
  font-size: 0.78rem;
  line-height: 1.55;
}

.strategy-suggestion-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  margin-top: 12px;
}

.channel-ai-assist {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  padding: 11px 12px;
  border: 1px dashed rgba(37, 104, 65, 0.3);
  border-radius: 10px;
  background: #f6faf4;
}

.channel-ai-assist strong {
  color: #315740;
}

.channel-ai-assist p {
  margin: 3px 0 0;
  color: #6c7b70;
  font-size: 0.76rem;
  line-height: 1.5;
}

.channel-ai-assist button {
  white-space: nowrap;
}

.channel-attribute-suggestion {
  padding: 13px 14px;
  border: 1px solid rgba(37, 104, 65, 0.2);
  border-radius: 11px;
  background: #f8fcf7;
}

.channel-attribute-suggestion dl {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 12px 0 0;
  color: #40554a;
  font-size: 0.8rem;
  line-height: 1.55;
}

.channel-attribute-suggestion dt {
  color: #6d7b71;
  font-weight: 700;
}

.channel-attribute-suggestion dd {
  margin: 0;
}

.channel-attribute-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
  margin-top: 12px;
}

.inline-check {
  display: flex !important;
  grid-template-columns: auto 1fr;
  align-items: center;
}

.inline-check input {
  width: auto;
}

.advanced-profile-settings {
  margin-top: 16px;
  padding: 14px;
  border-radius: 12px;
  background: rgba(235, 241, 233, 0.72);
}

.advanced-profile-settings summary {
  cursor: pointer;
  font-weight: 700;
}

.preview-layout {
  display: grid;
  grid-template-columns: minmax(220px, 260px) minmax(0, 1fr);
  gap: 14px;
}

.preview-layout.history-collapsed {
  grid-template-columns: minmax(0, 1fr);
}

.preview-history-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 10px;
  padding: 0 2px;
}

.preview-history-heading > div {
  display: grid;
  gap: 3px;
}

.preview-history-heading strong {
  color: #31523d;
}

.preview-history-heading small {
  color: #76847a;
  font-size: 0.74rem;
}

.tab-intro {
  margin-bottom: 14px;
  padding: 13px 15px;
  border: 1px solid rgba(37, 104, 65, 0.16);
  border-radius: 12px;
  background: #f2f8f0;
}

.tab-intro strong {
  color: #244f34;
}

.tab-intro p {
  margin: 5px 0 0;
  color: #607166;
  font-size: 0.8rem;
  line-height: 1.65;
}

.preview-plan {
  display: grid;
  gap: 12px;
}

.plan-total {
  display: grid;
  gap: 2px;
  padding-bottom: 11px;
  border-bottom: 1px solid rgba(37, 71, 50, 0.12);
}

.plan-total span {
  color: #587062;
  font-size: 0.76rem;
  font-weight: 700;
}

.plan-total strong {
  color: #1e5636;
  font-size: clamp(1.7rem, 4vw, 2.35rem);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.035em;
  line-height: 1.15;
}

.plan-total small {
  color: #78877d;
  font-size: 0.7rem;
}

.plan-details {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: #5f7166;
  font-size: 0.76rem;
}

.plan-reuse {
  color: #27623f;
  font-size: 0.76rem;
  font-weight: 650;
}

.document-picker {
  grid-column: 1 / -1;
  display: grid;
  gap: 9px;
  padding: 13px;
  border: 1px solid rgba(37, 71, 50, 0.13);
  border-radius: 12px;
  background: #f7faf5;
}

.document-picker-heading,
.aside-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.document-picker-heading > div:first-child {
  display: grid;
  gap: 3px;
}

.document-picker-heading small,
.aside-heading span {
  color: #748278;
  font-size: 0.72rem;
}

.document-picker-actions {
  display: flex;
  gap: 6px;
}

.document-picker-actions button {
  min-height: 30px;
  padding: 5px 9px;
  font-size: 0.72rem;
}

.document-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  max-height: 220px;
  overflow: auto;
  padding-right: 4px;
}

.document-options .document-check {
  margin-top: 0;
  padding: 8px 9px;
  border: 1px solid rgba(37, 71, 50, 0.1);
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.8);
}

.document-options .document-check > span {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.document-options strong {
  overflow: hidden;
  color: #334d3b;
  font-size: 0.78rem;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.document-options small {
  color: #77857b;
  font-size: 0.68rem;
}

.preview-run-list {
  align-self: start;
  display: grid;
  gap: 8px;
  max-height: 640px;
  overflow: auto;
}

.history-filters {
  display: grid;
  gap: 7px;
  margin-bottom: 5px;
}

.history-filters input,
.history-filters select {
  width: 100%;
  min-height: 34px;
  border: 1px solid rgba(37, 71, 50, 0.18);
  border-radius: 8px;
  padding: 7px 9px;
  color: #344e3d;
  background: rgba(255, 255, 255, 0.92);
  font: inherit;
  font-size: 0.76rem;
}

.aside-heading {
  padding: 2px 2px 6px;
}

.aside-heading strong {
  color: #365542;
  font-size: 0.82rem;
}

.aside-empty {
  margin: 0;
  padding: 14px 10px;
  border: 1px dashed rgba(37, 71, 50, 0.18);
  border-radius: 9px;
  color: #78867c;
  font-size: 0.76rem;
  line-height: 1.55;
  text-align: center;
}

.preview-run-list button,
.history-row {
  display: grid;
  gap: 4px;
  padding: 11px;
  border: 1px solid rgba(37, 71, 50, 0.12);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.76);
  text-align: left;
}

.preview-run-list button.active,
.preview-run-list button.active:hover {
  border-color: #2c7047;
  background: #e3f0e7;
}

.preview-run-list span,
.preview-run-list small {
  color: #6a7c70;
  font-size: 0.76rem;
}

.run-status-line.running {
  color: #236341;
  font-weight: 700;
}

.run-status-line.running::before {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-right: 6px;
  border-radius: 50%;
  background: #2b8453;
  box-shadow: 0 0 0 4px rgba(43, 132, 83, 0.14);
  content: "";
  animation: run-status-pulse 1.2s ease-in-out infinite;
}

.status-pill.status-running,
.status-pill.status-queued {
  color: #185f3a;
  background: #d8efe0;
}

.status-pill.status-failed,
.status-pill.status-interrupted {
  color: #8d3434;
  background: #f6dede;
}

.status-pill.status-cancelled {
  color: #6d6250;
  background: #ece6db;
}

.extraction-progress {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 14px;
  padding: 16px;
  border: 1px solid rgba(37, 104, 65, 0.2);
  border-radius: 12px;
  background:
    linear-gradient(90deg, rgba(222, 241, 227, 0.86), rgba(246, 251, 245, 0.94));
  overflow: hidden;
  position: relative;
}

.extraction-progress::after {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  height: 3px;
  background: linear-gradient(90deg, transparent, #2d7a4d, transparent);
  content: "";
  animation: extraction-progress-bar 1.4s ease-in-out infinite;
}

.extraction-progress strong {
  color: #245b39;
}

.extraction-progress p {
  margin: 4px 0 0;
  color: #617468;
  font-size: 0.78rem;
  line-height: 1.5;
}

.extraction-spinner {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border: 3px solid rgba(41, 110, 67, 0.2);
  border-top-color: #287247;
  border-radius: 50%;
  animation: extraction-spin 0.8s linear infinite;
}

@keyframes extraction-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes run-status-pulse {
  50% {
    opacity: 0.4;
    transform: scale(0.82);
  }
}

@keyframes extraction-progress-bar {
  0% {
    transform: translateX(-100%);
  }
  100% {
    transform: translateX(100%);
  }
}

.candidate-toolbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.candidate-browser-toolbar {
  display: grid;
  grid-template-columns:
    minmax(180px, 1.5fr)
    minmax(140px, 1fr)
    minmax(140px, 1fr)
    minmax(150px, 1fr)
    auto;
  gap: 9px;
  align-items: end;
  margin-top: 14px;
  padding: 12px;
  border: 1px solid rgba(37, 71, 50, 0.12);
  border-radius: 12px;
  background: #f7faf5;
}

.preview-view-switch {
  display: inline-flex;
  gap: 4px;
  margin-top: 14px;
  padding: 4px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 11px;
  background: #f4f8f2;
}

.preview-view-switch button {
  min-height: 32px;
  border: 0;
  padding: 6px 12px;
  background: transparent;
  box-shadow: none;
}

.preview-view-switch button.active,
.preview-view-switch button.active:hover {
  color: #fff;
  background: #285f3d;
}

.candidate-browser-toolbar label {
  display: grid;
  gap: 5px;
  min-width: 0;
  color: #5c6f63;
  font-size: 0.72rem;
}

.candidate-browser-toolbar input,
.candidate-browser-toolbar select {
  width: 100%;
  min-height: 34px;
  border: 1px solid rgba(37, 71, 50, 0.18);
  border-radius: 8px;
  padding: 7px 9px;
  color: #2e4937;
  background: rgba(255, 255, 255, 0.94);
  font: inherit;
  font-size: 0.76rem;
}

.candidate-workspace {
  display: grid;
  grid-template-columns: minmax(250px, 320px) minmax(0, 1fr);
  gap: 14px;
  margin-top: 14px;
  align-items: start;
}

.candidate-navigation {
  display: grid;
  gap: 12px;
  max-height: 720px;
  overflow: auto;
  padding: 12px;
  border: 1px solid rgba(37, 71, 50, 0.12);
  border-radius: 13px;
  background: #f7faf5;
}

.candidate-group {
  display: grid;
  gap: 6px;
}

.candidate-group-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: #3b5947;
}

.candidate-group-heading strong {
  overflow: hidden;
  font-size: 0.76rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-group-heading span {
  flex: 0 0 auto;
  border-radius: 999px;
  padding: 2px 7px;
  color: #486757;
  background: #e2ecdf;
  font-size: 0.67rem;
  font-weight: 700;
}

.candidate-navigation button {
  display: grid;
  gap: 3px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: rgba(255, 255, 255, 0.78);
  box-shadow: none;
  text-align: left;
}

.candidate-navigation button:hover:not(:disabled) {
  border-color: rgba(37, 104, 65, 0.24);
  transform: none;
}

.candidate-navigation button.active,
.candidate-navigation button.active:hover {
  border-color: #30774d;
  background: #e4f1e7;
  box-shadow: 0 4px 12px rgba(36, 87, 53, 0.08);
}

.candidate-navigation button strong {
  overflow: hidden;
  color: #2d4937;
  font-size: 0.78rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-navigation button small {
  display: -webkit-box;
  overflow: hidden;
  color: #6e7d73;
  font-size: 0.7rem;
  font-weight: 500;
  line-height: 1.4;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.candidate-navigation button span {
  color: #7a887f;
  font-size: 0.66rem;
}

.candidate-inspector {
  min-width: 0;
  padding: 18px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.78);
}

.candidate-inspector > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.candidate-inspector h4 {
  margin: 3px 0 4px;
  color: #203f2b;
  font-size: 1.15rem;
}

.candidate-inspector > header span {
  color: #728177;
  font-size: 0.75rem;
}

.candidate-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}

.candidate-meta span {
  border-radius: 999px;
  padding: 4px 8px;
  color: #496557;
  background: #e8f0e5;
  font-size: 0.68rem;
  font-weight: 650;
}

.candidate-alias-section {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f3f7f1;
}

.candidate-alias-section > strong {
  flex: 0 0 auto;
  padding-top: 3px;
  color: #53685a;
  font-size: 0.72rem;
}

.candidate-alias-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.candidate-alias-list span {
  border: 1px solid rgba(37, 104, 65, 0.16);
  border-radius: 999px;
  padding: 3px 8px;
  color: #315d42;
  background: #fff;
  font-family: Consolas, "Courier New", monospace;
  font-size: 0.7rem;
}

.candidate-alias-section > p {
  margin: 2px 0 0;
  color: #7b887f;
  font-size: 0.72rem;
}

.candidate-summary-section,
.candidate-evidence-section {
  margin-top: 14px;
  padding: 14px;
  border-radius: 11px;
}

.candidate-summary-section {
  border: 1px solid rgba(54, 97, 72, 0.12);
  background: #f7faf7;
}

.candidate-summary-section p {
  margin: 7px 0 0;
  color: #3e5447;
  line-height: 1.7;
}

.candidate-summary-section small {
  display: block;
  margin-top: 8px;
  color: #7b887f;
  font-size: 0.68rem;
}

.candidate-evidence-section {
  border-left: 4px solid #5b8ca2;
  background: #f3f8fa;
}

.source-order-view {
  margin-top: 14px;
}

.source-order-toolbar {
  display: grid;
  grid-template-columns: minmax(210px, 1fr) auto auto auto;
  gap: 14px;
  align-items: end;
  padding: 13px;
  border: 1px solid rgba(37, 71, 50, 0.13);
  border-radius: 12px;
  background: #f6faf4;
}

.source-order-toolbar > label:first-child {
  display: grid;
  gap: 6px;
  color: #52675a;
  font-size: 0.76rem;
}

.source-order-toolbar select {
  width: 100%;
  min-height: 35px;
  border: 1px solid rgba(37, 71, 50, 0.2);
  border-radius: 8px;
  padding: 7px 9px;
  color: #2f4938;
  background: #fff;
  font: inherit;
}

.source-order-stats {
  display: grid;
  gap: 2px;
  text-align: right;
}

.source-order-stats strong {
  color: #25613e;
  font-size: 1.1rem;
}

.source-order-stats span {
  color: #748278;
  font-size: 0.7rem;
}

.source-language-control {
  display: grid;
  gap: 5px;
  color: #52675a;
  font-size: 0.72rem;
}

.source-language-switch {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 9px;
  background: #edf4ea;
}

.source-language-switch button {
  min-height: 29px;
  border: 0;
  padding: 4px 10px;
  background: transparent;
  box-shadow: none;
  font-size: 0.72rem;
}

.source-language-switch button.active,
.source-language-switch button.active:hover {
  color: #fff;
  background: #2b6743;
}

.source-order-filter {
  white-space: nowrap;
}

.source-order-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  min-height: 180px;
  margin-top: 12px;
  border: 1px dashed rgba(37, 71, 50, 0.2);
  border-radius: 12px;
  color: #5c7063;
  background: #f8fbf7;
}

.source-comparison-list {
  display: grid;
  gap: 12px;
  margin-top: 14px;
}

.source-section-band {
  margin-top: 8px;
  border-bottom: 1px solid rgba(37, 71, 50, 0.16);
  padding: 7px 3px;
  color: #365943;
  font-size: 0.8rem;
  font-weight: 750;
  letter-spacing: 0.02em;
}

.source-comparison-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
  align-items: stretch;
}

.source-text-block,
.source-linked-candidates {
  min-width: 0;
  padding: 14px;
  border: 1px solid rgba(37, 71, 50, 0.12);
  border-radius: 12px;
}

.source-text-block {
  background: #fff;
}

.source-comparison-row.uncovered .source-text-block {
  border-color: rgba(184, 132, 38, 0.25);
  background: #fffdf7;
}

.source-block-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.source-block-heading span {
  color: #6c7d72;
  font-size: 0.7rem;
  font-weight: 700;
}

.source-block-heading code {
  overflow: hidden;
  color: #809086;
  font-size: 0.65rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-text-block > p {
  margin: 0;
  color: #30493a;
  font-family: Georgia, "Songti SC", serif;
  font-size: 0.88rem;
  line-height: 1.8;
  white-space: pre-wrap;
}

.source-block-translation {
  margin-top: 10px;
  padding: 9px 10px;
  border-left: 3px solid #5b8ca2;
  border-radius: 0 8px 8px 0;
  background: #f1f8fb;
}

.source-block-translation summary {
  cursor: pointer;
  color: #356477;
  font-size: 0.72rem;
  font-weight: 700;
}

.source-block-translation p {
  margin: 7px 0 0;
  color: #405d68;
  font-family: inherit;
  font-size: 0.78rem;
  line-height: 1.65;
}

.source-translation-missing {
  display: block;
  margin-top: 7px;
  color: #927231;
  font-size: 0.68rem;
}

.source-linked-candidates {
  display: grid;
  align-content: start;
  gap: 9px;
  background: #f5f9f4;
}

.source-linked-candidate {
  padding: 11px;
  border: 1px solid rgba(37, 104, 65, 0.14);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.9);
}

.source-linked-candidate header,
.source-linked-candidate footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.source-linked-candidate header > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.source-linked-candidate header strong {
  overflow: hidden;
  color: #254532;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-linked-candidate header small,
.source-linked-candidate footer span {
  color: #708077;
  font-size: 0.68rem;
}

.source-linked-candidate header button {
  min-height: 28px;
  padding: 4px 8px;
  font-size: 0.7rem;
  white-space: nowrap;
}

.source-linked-candidate > p {
  display: -webkit-box;
  overflow: hidden;
  margin: 8px 0;
  color: #506459;
  font-size: 0.76rem;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.source-linked-candidate footer button {
  min-height: 28px;
  padding: 4px 8px;
  color: #285f3d;
  font-size: 0.68rem;
  white-space: nowrap;
}

.source-linked-candidate.is-compact {
  border-style: dashed;
  padding: 8px 10px;
  background: rgba(239, 246, 236, 0.86);
}

.source-linked-candidate.is-compact header {
  align-items: center;
}

.source-linked-candidate.is-compact header small {
  color: #597360;
  font-style: italic;
}

.uncovered-block-note {
  display: grid;
  align-content: center;
  min-height: 100%;
  padding: 12px;
  border: 1px dashed rgba(184, 132, 38, 0.34);
  border-radius: 9px;
  color: #806628;
  background: #fffaf0;
}

.uncovered-block-note strong {
  font-size: 0.78rem;
}

.uncovered-block-note p {
  margin: 4px 0 0;
  color: #8b7540;
  font-size: 0.72rem;
  line-height: 1.5;
}

.profile-workbench button.translate-action {
  border-color: rgba(46, 99, 127, 0.25);
  color: #28566d;
  background: #eef7fb;
}

.profile-workbench button.translate-action:hover:not(:disabled) {
  border-color: rgba(46, 99, 127, 0.46);
  background: #e1f0f7;
}

.translation-notice {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  color: #255c3d;
  background: #e6f2e8;
  font-size: 0.76rem;
}

.candidate-card p {
  margin: 8px 0;
}

.candidate-card > div:first-child {
  display: grid;
  gap: 3px;
}

.candidate-card > div:first-child > span {
  color: #708176;
  font-size: 0.74rem;
}

.candidate-evidence {
  display: grid;
  gap: 6px;
  margin: 8px 0;
}

.candidate-evidence details {
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(225, 236, 223, 0.78);
}

.candidate-evidence summary {
  cursor: pointer;
  color: #345441;
  font-size: 0.76rem;
}

.candidate-evidence p {
  margin: 8px 0 6px;
  color: #52665a;
  font-size: 0.8rem;
}

.evidence-translation {
  margin: 8px 0;
  padding: 10px 11px;
  border-left: 3px solid #4d839b;
  border-radius: 0 8px 8px 0;
  background: #edf7fb;
}

.evidence-translation strong {
  color: #2e647a;
  font-size: 0.72rem;
}

.evidence-translation p {
  margin: 5px 0;
  color: #365462;
}

.evidence-translation small {
  color: #72848d;
  font-size: 0.68rem;
}

.candidate-evidence code {
  padding: 3px 6px;
  border-radius: 6px;
  background: #dfe9dd;
  font-size: 0.7rem;
}

.checklist-item-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 140px 120px auto;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.checklist-item-block {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid rgba(37, 71, 50, 0.1);
  border-radius: 10px;
  background: rgba(248, 251, 247, 0.78);
}

.checklist-item-block .checklist-item-row {
  margin-top: 0;
}

.checklist-evidence {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(228, 238, 226, 0.72);
}

.checklist-evidence summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  color: #405d49;
  font-size: 0.76rem;
  font-weight: 650;
}

.checklist-evidence summary span {
  border-radius: 999px;
  padding: 3px 7px;
  color: #28613e;
  background: #d5e9da;
  font-size: 0.66rem;
}

.checklist-evidence ul {
  display: grid;
  gap: 8px;
  margin: 9px 0 0;
  padding: 0;
  list-style: none;
}

.checklist-evidence li {
  padding: 8px 9px;
  border-radius: 7px;
  background: rgba(255, 255, 255, 0.7);
}

.checklist-evidence li strong {
  color: #3a5745;
  font-size: 0.72rem;
}

.checklist-evidence li p {
  margin: 4px 0 0;
  color: #5d6e63;
  font-size: 0.76rem;
  line-height: 1.55;
}

.field-note {
  display: block;
  color: #7b682f;
  font-size: 0.74rem;
  line-height: 1.55;
}

.manual-item-row {
  margin-top: 14px;
}

.manual-item-row input,
.manual-item-row select {
  flex: 1;
}

.comparison-builder {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.comparison-builder > div,
.comparison-builder > button {
  grid-column: 1 / -1;
}

.document-check {
  display: flex !important;
  align-items: center;
  gap: 8px !important;
  margin-top: 6px;
}

.document-check input {
  width: auto;
}

.comparison-count-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 12px 0;
}

.comparison-count-grid > div {
  display: grid;
  gap: 4px;
  padding: 12px;
  border-radius: 10px;
  background: rgba(226, 236, 224, 0.8);
}

.comparison-item {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.priority-chip {
  border-radius: 999px;
  padding: 3px 7px;
  color: #315f42;
  background: #dcecdf;
  font-size: 0.7rem;
  font-weight: 700;
}

.comparison-conclusion {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr) auto;
  align-items: end;
}

.comparison-conclusion label {
  display: grid;
  gap: 6px;
}

.history-section {
  display: grid;
  gap: 8px;
}

.history-row {
  grid-template-columns: 130px minmax(0, 1fr);
}

.empty-state {
  padding: 32px 18px;
  text-align: center;
  color: #697a6e;
}

.compiled-prompts pre {
  max-height: 360px;
  overflow: auto;
  padding: 12px;
  border-radius: 10px;
  background: #17251b;
  color: #e6f2e8;
  white-space: pre-wrap;
}

@media (max-width: 900px) {
  .profile-tabs {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 1200px) {
  .candidate-browser-toolbar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .candidate-browser-toolbar .candidate-toolbar-actions {
    grid-column: 1 / -1;
  }
}

@media (max-width: 1000px) {
  .candidate-workspace {
    grid-template-columns: 1fr;
  }

  .candidate-navigation {
    max-height: 300px;
  }

  .source-order-toolbar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .source-comparison-row {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 780px) {
  .strategy-generator {
    grid-template-columns: 1fr;
  }

  .strategy-generator-action {
    justify-items: start;
  }

  .channel-ai-assist {
    grid-template-columns: 1fr;
    justify-items: start;
  }

  .channel-attribute-suggestion dl {
    grid-template-columns: 1fr;
  }

  .channel-attribute-suggestion dt {
    margin-top: 4px;
  }

  .profile-status-row {
    align-items: stretch;
    flex-direction: column;
  }

  .profile-create-action {
    align-self: flex-end;
  }

  .profile-form-grid,
  .preview-layout,
  .document-options,
  .comparison-builder,
  .comparison-conclusion {
    grid-template-columns: 1fr;
  }

  .checklist-item-row {
    grid-template-columns: 1fr 1fr;
  }

  .profile-detail-dialog {
    width: calc(100vw - 24px);
    max-height: calc(100vh - 24px);
    padding: 18px;
  }

  .profile-detail-dialog > .section-heading {
    top: -18px;
    margin: -18px -18px 14px;
    padding: 16px 18px 13px;
  }

  .document-picker-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .candidate-toolbar-actions {
    width: 100%;
    justify-content: space-between;
    margin-left: 0;
  }

  .preview-history-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .candidate-workspace {
    grid-template-columns: 1fr;
  }

  .source-order-toolbar {
    grid-template-columns: 1fr;
  }

  .source-order-stats {
    text-align: left;
  }

  .source-candidate-dialog {
    padding: 18px;
  }

  .source-candidate-dialog-heading {
    top: -18px;
    margin: -18px -18px 0;
    padding: 18px 18px 13px;
  }

  .source-candidate-dialog-actions {
    flex-direction: column-reverse;
  }

  .candidate-inspector > header {
    align-items: stretch;
    flex-direction: column;
  }

  .info-tooltip > [role="tooltip"] {
    left: auto;
    right: -9px;
    width: min(260px, 78vw);
    transform: translate(0, 4px);
  }

  .info-tooltip:hover > [role="tooltip"],
  .info-tooltip:focus > [role="tooltip"],
  .info-tooltip:focus-within > [role="tooltip"] {
    transform: translate(0, 0);
  }

  .info-tooltip > [role="tooltip"]::after {
    right: 11px;
    left: auto;
    transform: translate(0, -4px) rotate(45deg);
  }
}
</style>
