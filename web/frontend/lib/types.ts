// PaperMine Web 前端类型定义。
// 与 M24 后端（web/api.py）与 Dossier schema（docs/architecture.md §4 + 各 Agent 产出）对齐。
// 所有字段均为可选：后端数据可能缺失 / 旧格式 / 离线降级，前端需稳健兜底。

// ---------- Dossier 顶层 ----------

export interface ProcessSignals {
  rollback_rounds?: number;
  degradations?: number;
}

export interface Meta {
  project_id?: string;
  version?: number;
  llm_backend?: string | null;
  prompt_versions?: Record<string, string>;
  run_id?: string;
  process_signals?: ProcessSignals;
}

export interface Facts {
  tasks?: string[];
  methods?: string[];
  data?: string[];
  scenarios?: string[];
  metrics?: string[];
  libraries?: string[];
  modules?: string[];
}

export interface Assets {
  facts?: Facts;
  narrative?: string;
  evidence?: { source?: string; snippet?: string }[];
}

export interface Problem {
  problem_id?: string;
  title?: string;
  formulation?: string;
  motivation?: string;
  why_not_engineering?: string;
  evidence_refs?: string[];
  provenance?: string;
}

// ---------- 文献 / 证据卡 / gap / 假设 ----------

export interface Understanding {
  claim?: string;
  method?: string;
  conclusion?: string;
  applicability?: string;
  limitations?: string;
}

export interface EvidenceCard {
  title?: string;
  dataset?: string | null;
  baseline?: string | null;
  metric?: string | null;
  main_gain?: string | null;
  limitation?: string | null;
  claim_strength?: string | null;
  evidence_source?: string;
}

export interface Paper {
  title?: string;
  abstract?: string;
  venue?: string;
  year?: number;
  source?: string;
  understanding?: Understanding;
  evidence_card?: EvidenceCard;
}

export interface GapHypothesis {
  claim?: string;
  evidence_level?: string;
  basis?: string;
  scope?: string;
}

export interface Gap {
  gap_id?: string;
  type?: "gap" | "contradiction";
  claim_point?: string;
  description?: string;
  angle?: string;
  paper_refs?: string[];
  gap_hypothesis?: GapHypothesis;
  /** 矛盾（contradiction）型为顶层 evidence_level（恒 strong） */
  evidence_level?: string;
}

export interface GraphNode {
  id?: string;
  label?: string;
  kind?: string;
}

export interface GraphEdge {
  source?: string;
  target?: string;
  kind?: string;
  claim_point?: string;
}

export interface ContradictionGraph {
  nodes?: GraphNode[];
  edges?: GraphEdge[];
  gaps?: Gap[];
}

export interface Hypothesis {
  hypothesis_id?: string;
  gap_ref?: string;
  statement?: string;
  falsification?: string;
}

export interface LiteratureEntry {
  query?: string;
  papers?: Paper[];
  gap_note?: string;
  sources?: string[];
  contradiction_graph?: ContradictionGraph;
  hypotheses?: Hypothesis[];
}

// ---------- 创新点 / 评估 / 贡献 ----------

export interface IdeaEvidence {
  source?: string;
  gap_id?: string;
  type?: string;
  evidence_level?: string;
  note?: string;
}

export interface IdeaHistoryEntry {
  ts?: string;
  action?: string;
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
  degraded?: boolean;
}

export interface Idea {
  idea_id?: string;
  claim?: string;
  novelty_hypothesis?: string;
  problem_ref?: string;
  literature_refs?: string[];
  gap_refs?: string[];
  hypothesis_refs?: string[];
  evidence?: IdeaEvidence[];
  status?: string;
  history?: IdeaHistoryEntry[];
}

export type Strength = "none" | "low" | "medium" | "medium_high" | "high";
export type ContributionType = "A" | "B" | "C" | "D" | "E";

export interface MatrixRow {
  strength?: Strength;
  label?: string;
  reason?: string;
}

export interface ContributionMatrix {
  method?: MatrixRow;
  framework?: MatrixRow;
  application?: MatrixRow;
  problem?: MatrixRow;
  training?: MatrixRow;
  engineering?: MatrixRow;
}

export interface Attack {
  attack?: string;
  answer?: string;
}

export interface Contribution {
  type?: ContributionType;
  type_label?: string;
  reason?: string;
  matrix?: ContributionMatrix;
  attacks?: {
    ablation?: Attack;
    concatenation?: Attack;
    reviewer?: Attack;
  };
  degraded?: boolean;
}

export interface NoveltyDim {
  score?: number;
  reason?: string;
}

export interface CalibrationQuestion {
  id?: string;
  text?: string;
  answer?: string;
  evidence?: string;
  rule?: string;
  effect?: string;
}

export interface CalibrationDim {
  label?: string;
  weight?: number;
  score?: number;
  base?: number;
  derivation?: string;
  questions?: CalibrationQuestion[];
}

export type EvidenceLevel = "weak" | "medium" | "strong";
export type CheckStatus = "ok" | "concern" | "missing";

export interface EvidenceCheck {
  status?: CheckStatus;
  note?: string;
}

export interface EvidenceValidation {
  evidence?: EvidenceLevel;
  reason?: string;
  checks?: {
    similar_work?: EvidenceCheck;
    theory_basis?: EvidenceCheck;
    experiment_support?: EvidenceCheck;
    claim_strength?: EvidenceCheck;
  };
  degraded?: boolean;
}

export type Verdict = "proceed" | "rework" | "drop";
export type Feasibility = "high" | "medium" | "low";

export interface Evaluation {
  idea_ref?: string;
  contribution?: Contribution;
  novelty_score?: number;
  novelty_band?: string;
  novelty_dimensions?: Record<string, NoveltyDim>;
  calibration?: Record<string, CalibrationDim>;
  evidence_validation?: EvidenceValidation;
  data_feasibility?: Feasibility;
  workload_hours?: number;
  venue_guess?: string;
  verdict?: Verdict;
  rework_reason?: string | null;
  evidence?: { source?: string; note?: string }[];
}

// ---------- 路线图（M22 七部分） ----------

export interface CoreStory {
  status_quo?: string;
  problem?: string;
  method?: string;
  contribution?: string;
}

export interface ResearchQuestion {
  id?: string;
  question?: string;
  target_experiments?: string[];
}

export interface Experiment {
  experiment?: string;
  purpose?: string;
  independent_variable?: string;
  baselines?: string[];
  metrics?: string[];
  rq?: string;
}

export interface MinimumViablePaper {
  must_have?: string[];
  optional?: string[];
}

export interface SuccessCriteria {
  success?: string[];
  failure?: string[];
  pivot?: string;
}

export interface RiskBranch {
  risk?: string;
  branch?: string;
}

export interface StageExit {
  stage?: string;
  tasks?: string[];
  exit_criteria?: string;
}

export interface Roadmap {
  selected_idea?: string | null;
  paper_type?: string;
  outline?: string[];
  core_story?: CoreStory;
  research_questions?: ResearchQuestion[];
  experiment_matrix?: Experiment[];
  minimum_viable_paper?: MinimumViablePaper;
  success_criteria?: SuccessCriteria;
  risk_branches?: RiskBranch[];
  stage_exits?: StageExit[];
  missing_items?: string[];
}

export interface HumanDecision {
  checkpoint?: string;
  decision?: string;
  note?: string;
  ts?: string;
}

export interface Dossier {
  meta?: Meta;
  assets?: Assets;
  problems?: Problem[];
  literature?: LiteratureEntry[];
  ideas?: Idea[];
  evaluations?: Evaluation[];
  roadmap?: Roadmap;
  human_decisions?: HumanDecision[];
}

// ---------- M24 API 响应 ----------

export interface ProjectSummary {
  project_id?: string;
  state?: string;
  updated_at?: string;
}

export interface RunStatus {
  state?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface ProjectPayload {
  project_id?: string;
  run_id?: string;
  status?: RunStatus | null;
  decision_report?: string;
  dossier?: Dossier;
}

export interface ProjectsResponse {
  projects: ProjectSummary[];
}

export interface IdeaWithEval {
  idea: Idea;
  evaluation?: Evaluation;
}

export interface IdeasResponse {
  ideas: IdeaWithEval[];
}

export interface GapPayload {
  gap_id?: string;
  type?: string;
  claim_point?: string;
  description?: string;
  angle?: string;
  paper_refs?: string[];
  evidence_level?: string | null;
  gap_hypothesis?: GapHypothesis;
  query?: string;
  coverage?: number;
  sources?: string[];
}

export interface GapsResponse {
  gaps: GapPayload[];
}

export interface LiteratureResponse {
  literature: LiteratureEntry[];
}

export interface RoadmapResponse {
  roadmap: Roadmap;
}

export interface SnapshotItem {
  file?: string;
  version?: number;
  evaluations?: {
    idea_ref?: string;
    novelty_score?: number;
    verdict?: string;
  }[];
}

export interface HistoryResponse {
  project_id?: string;
  status?: RunStatus | null;
  human_decisions?: HumanDecision[];
  snapshots?: SnapshotItem[];
}

export interface RefineResponse {
  idea: Idea;
  evaluation?: Evaluation;
  degraded?: boolean;
}

export interface EvaluateResponse {
  evaluation: Evaluation;
}

export interface RetrieveMoreResponse {
  gap?: GapPayload | Gap;
  added_papers?: string[];
  updated_evaluations?: Evaluation[];
}
