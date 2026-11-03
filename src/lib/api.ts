export type ComplaintCategory =
  | 'road_damage'
  | 'garbage'
  | 'water_leakage'
  | 'drainage'
  | 'streetlight'
  | 'sanitation'
  | 'other'

export type ComplaintSubmission = {
  category: ComplaintCategory
  description: string
  latitude: number
  longitude: number
  locationLabel: string
  isAnonymous: boolean
  images: File[]
}

export type ComplaintReceipt = {
  complaint_id: string
  public_reference: string
  tracking_token: string
  status: string
  emergency_flag: boolean
}

export type ComplaintTracking = {
  public_reference: string
  category: string
  status: string
  submitted_at: string
  updated_at: string
  history: {
    status: string
    note: string | null
    created_at: string
  }[]
  master_issue_reference?: string | null
  master_issue_status?: string | null
  report_count?: number | null
}

export type SavedComplaint = ComplaintTracking & {
  tracking_token: string
}

export type PublicIssue = {
  id: string
  public_reference: string
  category: string
  title: string
  priority: string
  status: string
  report_count: number
  action_count: number
  created_at: string
  updated_at: string
  resolved_at: string | null
}

export type PublicDashboardSummary = {
  open_master_issues: number
  duplicate_reports: number
  critical_issues: number
  resolved_issues: number
}

export type StaffComplaint = {
  id: string
  public_reference: string
  category: string
  description: string
  location_label: string | null
  status: string
  emergency_flag: boolean
  submitted_at: string
  updated_at: string
  master_issue_id?: string | null
  master_issue_reference?: string | null
  master_issue_status?: string | null
  priority?: string | null
  assigned_department_id?: string | null
  assigned_department_name?: string | null
  report_count: number
  evidence: { storage_path: string; content_type: string; byte_size: number; malware_scan_status: string; created_at: string }[]
  duplicate_candidates: { id: string; candidate_master_issue_id: string; text_similarity: number | null; location_similarity: number | null; combined_score: number | null; decision: string; created_at: string }[]
  history: { from_status: string | null; to_status: string; note: string | null; changed_by_firebase_uid: string | null; created_at: string }[]
}

export type AnalyticsOverview = {
  complaints: { total: number; by_status: Record<string, number>; emergency: number }
  master_issues: { total: number; by_status: Record<string, number>; by_priority: Record<string, number> }
  escalations: { total: number; overdue_sla: number; by_status: Record<string, number> }
}

export type Escalation = {
  id: string
  master_issue_id: string
  level: number
  reason: string
  authority: string
  status: string
  due_at: string | null
  sla_due_at: string | null
  escalated_at: string
  resolved_at: string | null
}

export type EmergencyReview = {
  id: string
  complaint_id: string
  severity: string
  review_status: string
  review_note: string | null
  reviewed_at: string | null
  created_at: string
  complaint?: { public_reference: string; category: string; description: string; status: string }
}

export type FinancialSummary = {
  record_count: number
  totals_by_currency_category: Record<string, number>
  totals_by_master_issue: Record<string, number>
}

export type AppNotification = {
  id: string
  complaint_id: string | null
  title: string
  body: string
  read_at: string | null
  created_at: string
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
let authToken: string | null = null

export type AuthenticatedUser = {
  firebase_uid: string
  email: string | null
  display_name: string | null
  role: 'citizen' | 'officer' | 'supervisor' | 'administrator' | 'auditor'
  is_staff: boolean
}

export function setAuthToken(token: string | null): void {
  authToken = token
}

export async function getCurrentUser(token = authToken): Promise<AuthenticatedUser> {
  const response = await fetch(`${apiBaseUrl}/api/v1/auth/me`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new Error('Your session could not be verified.')
  return response.json() as Promise<AuthenticatedUser>
}

async function staffRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
      'Content-Type': 'application/json',
    },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail ?? 'Staff operation failed.')
  }
  return response.json() as Promise<T>
}

export function listStaffComplaints(status?: string): Promise<StaffComplaint[]> {
  return staffRequest<StaffComplaint[]>(`/api/v1/admin/complaints${status ? `?status_filter=${encodeURIComponent(status)}` : ''}`)
}

export function updateStaffComplaintStatus(id: string, complaintStatus: string, note: string): Promise<StaffComplaint> {
  return staffRequest<StaffComplaint>(`/api/v1/admin/complaints/${id}/status`, {
    method: 'PATCH',
    body: JSON.stringify({ status: complaintStatus, note }),
  })
}

export function listStaffDepartments(): Promise<{ id: string; name: string; description: string | null }[]> {
  return staffRequest<{ id: string; name: string; description: string | null }[]>('/api/v1/admin/departments')
}

export function assignStaffComplaint(id: string, departmentId: string): Promise<StaffComplaint> {
  return staffRequest<StaffComplaint>(`/api/v1/admin/complaints/${id}/assignment`, {
    method: 'POST',
    body: JSON.stringify({ department_id: departmentId }),
  })
}

export function reviewDuplicateCandidate(id: string, decision: string, note: string): Promise<StaffComplaint> {
  return staffRequest<StaffComplaint>(`/api/v1/admin/duplicate-candidates/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ decision, note }),
  })
}

export function getStaffEvidence(id: string): Promise<{ storage_path: string; content_type: string; byte_size: number; malware_scan_status: string; created_at: string; signed_url?: string }[]> {
  return staffRequest(`/api/v1/admin/complaints/${id}/evidence`)
}

export function listNotifications(): Promise<AppNotification[]> {
  return staffRequest<AppNotification[]>('/api/v1/notifications')
}

export function getAnalyticsOverview(): Promise<AnalyticsOverview> {
  return staffRequest('/api/v1/admin/analytics/overview')
}

export function listEscalations(status?: string): Promise<Escalation[]> {
  return staffRequest<Escalation[]>(`/api/v1/admin/escalations${status ? `?escalation_status=${encodeURIComponent(status)}` : ''}`)
}

export function listEmergencyReviews(status = 'pending'): Promise<EmergencyReview[]> {
  return staffRequest<EmergencyReview[]>(`/api/v1/admin/emergency-reviews?review_status=${encodeURIComponent(status)}`)
}

export function updateEmergencyReview(id: string, reviewStatus: string, reviewNote?: string): Promise<EmergencyReview> {
  return staffRequest<EmergencyReview>(`/api/v1/admin/emergency-reviews/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ review_status: reviewStatus, review_note: reviewNote ?? null }),
  })
}

export function getFinancialSummary(): Promise<FinancialSummary> {
  return staffRequest('/api/v1/admin/financial-summary')
}

export function getDuplicateCandidateDetail(id: string): Promise<Record<string, unknown>> {
  return staffRequest(`/api/v1/admin/duplicate-candidates/${id}`)
}

export function mergeComplaint(masterIssueId: string, complaintId: string, reason?: string): Promise<{ complaint_id: string; master_issue_id: string; previous_master_issue_id: string }> {
  return staffRequest(`/api/v1/admin/master-issues/${masterIssueId}/merge`, {
    method: 'POST',
    body: JSON.stringify({ complaint_id: complaintId, reason: reason ?? null }),
  })
}

export function unmergeComplaint(masterIssueId: string, complaintId: string, reason?: string): Promise<{ complaint_id: string; master_issue_id: string; restored: boolean }> {
  return staffRequest(`/api/v1/admin/master-issues/${masterIssueId}/unmerge`, {
    method: 'POST',
    body: JSON.stringify({ complaint_id: complaintId, reason: reason ?? null }),
  })
}

export function getPriorityAnalytics(): Promise<Record<string, unknown>> {
  return staffRequest('/api/v1/admin/analytics/priority')
}

export function getPriorityHistory(masterIssueId: string): Promise<Record<string, unknown>[]> {
  return staffRequest(`/api/v1/admin/master-issues/${masterIssueId}/priority-history`)
}

export function getRootCauses(masterIssueId: string): Promise<Record<string, unknown>[]> {
  return staffRequest(`/api/v1/admin/master-issues/${masterIssueId}/root-cause`)
}

export async function submitComplaint(payload: ComplaintSubmission): Promise<ComplaintReceipt> {
  const formData = new FormData()
  formData.append('category', payload.category)
  formData.append('description', payload.description)
  formData.append('latitude', String(payload.latitude))
  formData.append('longitude', String(payload.longitude))
  formData.append('location_label', payload.locationLabel)
  formData.append('is_anonymous', String(payload.isAnonymous))
  payload.images.forEach((image) => formData.append('images', image))

  const response = await fetch(`${apiBaseUrl}/api/v1/complaints`, {
    method: 'POST',
    headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
    body: formData,
  })

  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string | { msg?: string }[] } | null
    const detail = body?.detail
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg ?? 'Invalid value').join('; ')
      : detail
    throw new Error(message ?? 'The complaint could not be submitted.')
  }

  return response.json() as Promise<ComplaintReceipt>
}

export async function trackComplaint(trackingToken: string): Promise<ComplaintTracking> {
  const response = await fetch(`${apiBaseUrl}/api/v1/complaints/track`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    },
    body: JSON.stringify({ tracking_token: trackingToken }),
  })

  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail ?? 'The complaint could not be found.')
  }

  return response.json() as Promise<ComplaintTracking>
}

export async function listPublicIssues(): Promise<PublicIssue[]> {
  const response = await fetch(`${apiBaseUrl}/api/v1/public/issues`)
  if (!response.ok) {
    throw new Error('Public issues could not be loaded.')
  }

  return response.json() as Promise<PublicIssue[]>
}

export async function getPublicDashboardSummary(): Promise<PublicDashboardSummary> {
  const response = await fetch(`${apiBaseUrl}/api/v1/public/summary`)
  if (!response.ok) {
    throw new Error('Dashboard summary could not be loaded.')
  }
  return response.json() as Promise<PublicDashboardSummary>
}
