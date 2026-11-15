export interface Identity { subject: string; name: string; admin: boolean; email?: string }
export interface Run { id: string; expert_name: string; since: string; until: string; status: string; can_manage: boolean }
export const sections = ['trigger', 'diagnosis', 'steps', 'rollback', 'contacts', 'pitfalls', 'rationale'] as const;
export type SectionName = typeof sections[number];
export type SectionStatus = 'missing' | 'noted' | 'open' | 'confidential';
export interface Question { text: string; origin: 'drafted' | 'expert'; artifact_id: string | null; quote: string | null }
export interface Topic { topic_id: string; label: string; kind: string; system: string | null; origin: 'ranked' | 'contradiction' | 'expert'; score: number | null; reason: string; artifact_ids: string[]; questions: Question[]; dropped: string[]; generation_state?: 'completed' | 'pending' }
export interface Plan { id: string; run_id: string; status: 'draft' | 'approved'; topics: Topic[]; approved_by: string | null; approved_at: string | null }
export interface PlanSummary { id: string; status: 'draft' | 'approved'; session_id: string | null }
export interface Generation { id: string; run_id: string; status: 'running' | 'needs_retry' | 'completed'; completed: number; total: number; plan_id: string | null; topics: Topic[] }
export interface Turn { ordinal: number; topic_id: string | null; speaker: 'interviewer' | 'expert'; kind: 'question' | 'answer' | 'skip' | 'correction' | 'edit'; text: string; confidential: boolean; section: SectionName | null; answers: number | null; corrects: number | null }
export interface Note { topic_id: string; section: SectionName; text: string; turn: number; confidential: boolean }
export interface OpenItem { topic_id: string; section: SectionName; reason: string; turn: number | null }
export interface Waiting { turn: number; question: string; section: SectionName; topic_id: string }
export interface Session { id: string; plan_id: string; run_id: string; status: 'active' | 'finished'; current_topic_id: string | null; waiting: Waiting | null; checklist: Record<SectionName, SectionStatus>; turns: Turn[]; notes: Note[]; open_items: OpenItem[] }
export interface Receipt { id: string; turn: number; status: 'saved' | 'processing' | 'needs_retry' | 'processed'; question_turn: number | null; corrects: number | null; result: StreamEvent | null }
export interface StreamEvent { type: 'delivery' | 'topic' | 'note' | 'open' | 'corrected' | 'recovered' | 'question' | 'done' | 'error'; id?: string; status?: Receipt['status']; topic_id?: string; label?: string; section?: SectionName; text?: string; reason?: string; confidential?: boolean; turn?: number; corrects?: number; sections?: SectionName[]; question?: string; code?: string; message?: string; message_id?: string }
export type Citation = { kind: 'interview_turn'; session_id: string; turn_index: number } | { kind: 'artifact'; artifact_id: string; quote?: string | null };
export interface Claim { text: string; citations: Citation[] }
export interface Section { claims: Claim[]; open_question: string | null }
export interface EntrySummary { id: string; topic_id?: string; title: string; status: 'draft' | 'approved' | 'rejected'; version: number; edited?: boolean; unsupported_count?: number; claim_count?: number; open_question_count?: number }
export type Entry = EntrySummary & Record<SectionName, Section> & { summary?: string; approved_by: string | null; approved_at: string | null; session_id?: string; rejection_reason?: string | null; unsupported?: { section: SectionName; claim_index: number; terms: string[] }[]; decisions?: { action: 'approve' | 'reject' | 'edit' | 'correction'; who: string; at: string; reason: string | null }[] };
export interface Evidence { id: string; entry_id: string; title: string; version: number; section: SectionName; claim_index: number; text: string; citations: Citation[] }
export interface Answer { status: 'answered' | 'insufficient_evidence'; answer: string; evidence: Evidence[]; open_questions: { entry_id: string; version: number; section: SectionName; text: string }[]; mode: 'extractive' | 'model_selected' }
export interface SearchResult { id: string; title: string; version: number; section: SectionName; snippet: string; score: number; citations: Citation[] }
