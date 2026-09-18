export type GrillSessionStatus =
  | 'intake_pending'
  | 'interviewing'
  | 'ready_for_confirmation'
  | 'analyzing'
  | 'completed'
  | 'failed';

export interface GrillOption {
  label: string;
  interpretation: string;
}

export interface GrillQuestion {
  id: string;
  text: string;
  target_ids?: string[];
  why?: string;
  options: GrillOption[];
  free_text?: boolean;
  allow_unknown?: boolean;
}

export interface CustomerAnswer {
  question_id: string;
  selected_option?: string | null;
  free_text_answer?: string | null;
  is_unknown?: boolean;
}

export interface BehaviorTreeNode {
  id: string;
  name: string;
  type: 'sequence' | 'fallback' | 'action' | 'condition' | 'decorator';
  description?: string;
  parent_id?: string | null;
  children?: string[];
  status?: string;
}

export interface ScenarioState {
  schema_version?: string;
  scenario_id?: string;
  revision: number;
  summary: string;
  root_id?: string;
  nodes: BehaviorTreeNode[];
  fields?: Array<{ id: string; name: string; value?: unknown; confidence?: string }>;
  issues?: Array<{ id: string; message: string; severity?: string }>;
  questions?: GrillQuestion[];
  ready_for_readback?: boolean;
}

export interface CapabilityClaim {
  claim_id: string;
  title: string;
  category: string;
  status: 'verified' | 'feasible' | 'gap' | 'unsupported';
  statement: string;
  citations?: string[];
}

export interface ArchNode {
  name: string;
  package: string;
  type: string;
  topics_sub?: string[];
  topics_pub?: string[];
}

export interface RiskItem {
  risk_id: string;
  title: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  likelihood: 'low' | 'medium' | 'high';
  mitigation: string;
  citations?: string[];
}

export interface GrillReport {
  schema_version: string;
  session_id: string;
  scenario_summary: string;
  behavior_tree?: {
    root_id: string;
    nodes: BehaviorTreeNode[];
  };
  capabilities?: {
    summary?: string;
    claims?: CapabilityClaim[];
  };
  system_architecture?: {
    summary?: string;
    nodes?: ArchNode[];
    middleware?: string;
    recommendations?: string[];
  };
  risk_matrix?: {
    summary?: string;
    risks?: RiskItem[];
  };
}

export interface GrillSessionFile {
  id: string;
  name: string;
  size: number;
  mime_type: string;
  sha256?: string;
}

export interface GrillTurn {
  id: number;
  turn_index: number;
  questions: GrillQuestion[];
  answers: CustomerAnswer[];
  created_at: string;
}

export interface GrillSession {
  id: string;
  status: GrillSessionStatus;
  task_intent: string;
  referenced_robot: string | null;
  question_count: number;
  current_revision: number;
  scenario_state: ScenarioState | null;
  active_questions: GrillQuestion[];
  readback_summary: string | null;
  final_report: GrillReport | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error_message: string | null;
  files?: GrillSessionFile[];
  turns?: GrillTurn[];
  token?: string;
  container_state?: string;
  setup_stage?: string;
  setup_message?: string;
  active_task_id?: string;
}

export interface SessionCreateResponse {
  token: string;
  session_url: string;
  session: GrillSession;
}
