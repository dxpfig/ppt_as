import type {
	Project,
	Slide,
	Template,
	Artifact,
	SlideImage,
	ImageSource,
	PptdDocument,
} from './model';

export type ImageStatus = {
	weknoraConfigured: boolean;
	webProviders: string[];
	mcpConfigured: boolean;
	weknoraConnected?: boolean;
	knowledgeBases?: { id: string; name: string }[];
	warning?: string;
};
export type Health = {
	images?: ImageStatus;
	llmConfigured: boolean;
	model: string;
	provider?: string;
	modelAvailable?: boolean;
	modelError?: string;
	rendererAvailable: boolean;
};
export type ReferenceBudget = {
	available: boolean;
	provider: string;
	model: string;
	contextTokens: number;
	outputTokens: number;
	safetyTokens: number;
	inputTokens: number;
	charactersPerToken: number;
	maxCharacters: number;
	maxFileBytes: number;
	source: string;
	counting: string;
	warning?: string;
};
export type ReferenceFile = {
	id: string;
	name: string;
	size: number;
	extension: string;
	characters: number;
	createdAt: number;
	downloadUrl: string;
	textUrl: string;
};
export type ProjectSummary = {
	id: string;
	title: string;
	slideCount: number;
	updatedAt?: number;
	phase?: string;
};
export type PageActionIssue = {
	pagePath: string;
	pageIndex?: number;
	elementIds?: string[];
	elementId?: string;
	severity?: 'error' | 'warning';
	code: string;
	message: string;
	evidence?: string;
	repair?: string;
	location?: string;
};
export type PageActionReport = {
	action: 'review' | 'repair' | 'design';
	pagePath: string;
	revision: number;
	status: 'passed' | 'issues';
	issues: PageActionIssue[];
	observations?: { pagePath: string; evidence: string }[];
	model?: string;
	candidate?: {
		id: string;
		baseRevision: number;
		previewUrl: string;
		applyUrl: string;
		discardUrl: string;
	};
};
export type StudioJob = {
	id: string;
	kind?:
		| 'outline'
		| 'revise'
		| 'render'
		| 'kimi-slides'
		| 'page-repair'
		| 'page-review'
		| 'page-design';
	status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
	stage: number;
	message: string;
	createdAt?: number;
	updatedAt?: number;
	execution?: JobExecution;
	diagnostic?: {
		code?: string;
		phase?: string;
		issues?: {
			page?: string;
			pageIndex?: number;
			pageTitle?: string;
			location: string;
			elementId?: string;
			code: string;
			message: string;
		}[];
	};
	result?: {
		slides?: Slide[];
		title?: string;
		reply?: string;
		artifact?: Artifact;
		project?: Project;
		warnings?: {
			location?: string;
			code?: string;
			message: string;
		}[];
		pageAction?: PageActionReport;
	};
	error?: string;
	recoverable?: boolean;
	checkpoint?: { summary?: string; stage?: string; [key: string]: unknown } | string;
	traceId?: string;
	events?: {
		message?: string;
		stage?: number;
		timestamp?: number | string;
		[key: string]: unknown;
	}[];
};
export type ExecutionPhase =
	| 'context'
	| 'plan'
	| 'assets'
	| 'pages'
	| 'validate'
	| 'export'
	| 'visual'
	| 'done';
export type ExecutionStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
export type ExecutionNode = {
	id: string;
	label: string;
	status: ExecutionStatus;
	startedAt?: number;
	endedAt?: number;
	summary?: string;
};
export type ExecutionPage = {
	path: string;
	index: number;
	title: string;
	status: ExecutionStatus;
	attempt?: number;
	message?: string;
	updatedAt?: number;
	issueCount?: number;
	elementId?: string;
};
export type ExecutionEvent = {
	timestamp?: number | string;
	phase?: string;
	status?: string;
	message: string;
	pagePath?: string;
	pageIndex?: number;
	elementId?: string;
	attempt?: number;
	scope?: 'deck' | 'page' | 'repair';
	issueCount?: number;
	covered?: number;
	total?: number;
};
export type VisualExecution = {
	attempt?: number;
	scope?: 'deck' | 'page' | 'repair';
	status?: ExecutionStatus;
	covered?: number;
	total?: number;
	issueCount?: number;
};
export type JobExecution = {
	phase?: string;
	nodes?: ExecutionNode[];
	pages?: ExecutionPage[];
	visualPages?: ExecutionPage[];
	visual?: VisualExecution;
	events?: ExecutionEvent[];
};
export type Workflow = 'new' | 'edit' | 'recreate';
export type DesignMode = 'autonomous' | 'design-system' | 'template' | 'style-reference';
export type Scenario =
	| 'auto'
	| 'management-report'
	| 'analysis-decision'
	| 'business-plan'
	| 'tech-engineering'
	| 'academic-research'
	| 'education-training'
	| 'brand-creative';
export type JobRequest = {
	kind:
		| 'outline'
		| 'revise'
		| 'render'
		| 'kimi-slides'
		| 'page-repair'
		| 'page-review'
		| 'page-design';
	project: Project;
	prompt?: string;
	pageCount?: number | null;
	workflow?: Workflow;
	designMode?: DesignMode;
	usage?: 'reading' | 'presentation';
	allowResearch?: boolean;
	speakerNotes?: boolean;
	animationRequested?: boolean;
	designSystem?: string;
	scenario?: Scenario;
	expectedRevision?: number;
	attachments?: { name: string; imageData: string }[];
	audience?: string;
	selectedSlideId?: string;
	references?: string;
	templateId?: string;
};
export class StudioError extends Error {
	readonly status: number;
	constructor(message: string, status = 0) {
		super(message);
		this.status = status;
	}
}
async function request<T>(path: string, options: RequestInit = {}, timeout = 30000): Promise<T> {
	let response: Response;
	try {
		response = await fetch(`/api/studio${path}`, {
			...options,
			signal: AbortSignal.timeout(options.body instanceof FormData ? 120000 : timeout),
		});
	} catch {
		throw new StudioError('无法连接服务，请检查网络或后端是否启动。');
	}
	if (!response.ok) {
		const content = await response.text();
		let message = `请求失败（${response.status}），请稍后重试。`;
		try {
			const data = JSON.parse(content) as { detail?: unknown; error?: string };
			if (typeof data.detail === 'string') message = data.detail;
			else if (Array.isArray(data.detail)) {
				const first = data.detail[0] as
					| { loc?: (string | number)[]; msg?: string }
					| undefined;
				if (first?.msg)
					message = `输入内容不符合要求：${first.loc?.filter((part) => part !== 'body').join('.') ?? ''} ${first.msg}`;
			} else if (data.error) message = data.error;
		} catch {
			/* Proxy HTML errors are not helpful to users. */
		}
		throw new StudioError(message, response.status);
	}
	return response.json() as Promise<T>;
}
const json = (method: string, body: unknown): RequestInit => ({
	method,
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify(body),
});
export const studioApi = {
	imageStatus: () => request<ImageStatus>('/images/status'),
	searchImages: (query: string, source: Exclude<ImageSource, 'none'>) =>
		request<{ images: SlideImage[]; warnings: string[] }>(
			'/images/search',
			json('POST', { query, source, limit: 4 }),
			100000,
		),
	health: () => request<Health>('/health'),
	referenceBudget: () => request<ReferenceBudget>('/reference-budget', {}, 35000),
	referenceFiles: (projectId: string) =>
		request<ReferenceFile[]>(`/projects/${encodeURIComponent(projectId)}/reference-files`),
	uploadReferenceFile: (projectId: string, file: File, text: string) => {
		const form = new FormData();
		form.append('file', file);
		form.append('text', text);
		return request<ReferenceFile>(
			`/projects/${encodeURIComponent(projectId)}/reference-files`,
			{ method: 'POST', body: form },
			120000,
		);
	},
	referenceFileText: (projectId: string, attachmentId: string) =>
		request<ReferenceFile & { text: string }>(
			`/projects/${encodeURIComponent(projectId)}/reference-files/${encodeURIComponent(attachmentId)}/text`,
		),
	deleteReferenceFile: (projectId: string, attachmentId: string) =>
		request<{ id: string; deleted: boolean }>(
			`/projects/${encodeURIComponent(projectId)}/reference-files/${encodeURIComponent(attachmentId)}`,
			{ method: 'DELETE' },
		),
	project: (id: string) => request<Project>(`/projects/${encodeURIComponent(id)}`),
	projects: () => request<ProjectSummary[]>('/projects'),
	deleteProject: (id: string) =>
		request<{ id: string; deleted: boolean }>(`/projects/${encodeURIComponent(id)}`, {
			method: 'DELETE',
		}),
	restoreProject: (id: string) =>
		request<ProjectSummary>(`/projects/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
	save: (project: Project) =>
		request<Project>(`/projects/${encodeURIComponent(project.id)}`, json('PUT', project)),
	templates: () => request<Template[]>('/templates'),
	uploadTemplate: (file: File) => {
		const form = new FormData();
		form.append('file', file);
		return request<Template>('/templates', { method: 'POST', body: form });
	},
	start: (body: JobRequest) => request<StudioJob>('/jobs', json('POST', body)),
	job: (id: string) => request<StudioJob>(`/jobs/${encodeURIComponent(id)}`),
	retry: (id: string) =>
		request<StudioJob>(`/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
	resume: (id: string) =>
		request<StudioJob>(`/jobs/${encodeURIComponent(id)}/resume`, { method: 'POST' }),
	pptd: (id: string) => request<PptdDocument>(`/projects/${encodeURIComponent(id)}/pptd`),
	migratePptd: (id: string) =>
		request<unknown>(`/projects/${encodeURIComponent(id)}/pptd/migrate`, { method: 'POST' }),
	importTemplate: (id: string, templateId: string, expectedRevision?: number) =>
		request<unknown>(
			`/projects/${encodeURIComponent(id)}/pptd/import-template`,
			json('POST', { templateId, expectedRevision }),
			120000,
		),
	restorePptd: (id: string, versionId: string, expectedRevision: number) =>
		request<unknown>(
			`/projects/${encodeURIComponent(id)}/pptd/versions/${encodeURIComponent(versionId)}/restore`,
			json('POST', { expectedRevision }),
		),
	applyDesignCandidate: (id: string, candidateId: string, expectedRevision: number) =>
		request<unknown>(
			`/projects/${encodeURIComponent(id)}/pptd/design-candidates/${encodeURIComponent(candidateId)}/apply`,
			json('POST', { expectedRevision }),
		),
	discardDesignCandidate: (id: string, candidateId: string) =>
		request<{ id: string; discarded: boolean }>(
			`/projects/${encodeURIComponent(id)}/pptd/design-candidates/${encodeURIComponent(candidateId)}`,
			{ method: 'DELETE' },
		),
	cancel: (id: string) =>
		request<StudioJob>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
};
