export type SlideImage = {
	assetId: string;
	url: string;
	title: string;
	provider: string;
	sourceUrl: string;
	author: string;
	license: string;
	width: number;
	height: number;
};
export type ImageSource = 'auto' | 'knowledge' | 'web' | 'none';
export const slideLayoutNames = {
	auto: '自动选择',
	cover: '开场封面',
	editorial: '重点阐述',
	image: '图文展示',
	comparison: '双项对比',
	process: '步骤流程',
	closing: '结论与行动',
};
export type SlideLayout = keyof typeof slideLayoutNames;
export type Slide = {
	layout?: SlideLayout;
	section?: string;
	source?: string;
	imageIntent?: 'auto' | 'photo' | 'diagram' | 'none';
	imageQuery?: string;
	imageDisabled?: boolean;
	image?: SlideImage | null;
	id: string;
	title: string;
	subtitle: string;
	points: string[];
	notes: string;
};
export type Message = { id: string; role: 'user' | 'assistant'; text: string };
export type Theme = 'forest' | 'ink' | 'clay';
export type Phase = 'outline' | 'generating' | 'ready';
export type Artifact = {
	id: string;
	revision?: number;
	pptdRevision?: number;
	pptxUrl: string;
	pdfUrl?: string;
	previewUrls: string[];
	warnings: string[];
};
export type PptdVersion = {
	id: string;
	revision: number;
	label: string;
	createdAt?: string;
	pageCount?: number;
};
export type PptdPage = { path: string; data: Record<string, unknown> };
export type PptdDocument = {
	document: Record<string, unknown>;
	pages: PptdPage[];
	revision: number;
	versions: PptdVersion[];
};
export type PptdReference = {
	revision: number;
	pageCount: number;
	entry?: string;
	pages?: { path: string; title: string }[];
	viewUrl: string;
	sourceUrl: string;
	versions?: PptdVersion[];
};
export type Template = {
	id: string;
	name: string;
	slideCount: number;
	layouts: string[];
	warnings: string[];
	previewUrls?: string[];
};
export type Version = {
	imageSource?: ImageSource;
	id: string;
	title?: string;
	label: string;
	slides: Slide[];
	theme: Theme;
	templateId?: string;
	artifact?: Artifact;
};
export type VisualReviewSummary = {
	status: 'passed' | 'failed';
	rounds: number;
	reviewedPages: number;
	totalPages: number;
	issueCount: number;
	repairedIssueCount?: number;
	summary?: string;
	model?: string;
};
export type Project = {
	generationMeta?: { validation?: { visualReview?: VisualReviewSummary } };
	workflow?: 'pptd-v2';
	pptd?: PptdReference;
	importWarnings?: string[];
	imageSource?: ImageSource;
	pageCount?: number;
	audience?: string;
	references?: string;
	activeJobId?: string | null;
	lastJobId?: string | null;
	contentGenerated?: boolean;
	id: string;
	title: string;
	slides: Slide[];
	messages: Message[];
	theme: Theme;
	phase: Phase;
	versions: Version[];
	templateId?: string;
	artifact?: Artifact;
};
export const uid = () =>
	crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
export const initialProject = (): Project => ({
	id: uid(),
	title: '未命名演示',
	contentGenerated: false,
	theme: 'forest',
	phase: 'outline',
	versions: [],
	messages: [],
	slides: [],
});
export const isEmptyProject = (project: Project) =>
	!project.pptd &&
	!project.slides.length &&
	!project.messages.length &&
	!project.templateId &&
	!project.versions.length &&
	project.title === '未命名演示';
const STORAGE_KEY = 'ppt-as-studio-v3';
export function loadProject(): Project {
	try {
		// One-time reset of the prototype's demo cache, including resumable test jobs.
		if (!localStorage.getItem('ppt-as-clean-start-v3')) {
			for (const key of [
				'ppt-as-studio-v1',
				'ppt-as-studio-v2',
				'ppt-as-active-job',
				'ppt-as-recovery-draft',
			]) {
				localStorage.removeItem(key);
			}
			localStorage.setItem('ppt-as-clean-start-v3', '1');
		}
		const project = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as Project;
		if (
			project &&
			typeof project.title === 'string' &&
			['forest', 'ink', 'clay'].includes(project.theme) &&
			Array.isArray(project.slides) &&
			project.slides.every(
				(slide) =>
					slide &&
					typeof slide.id === 'string' &&
					typeof slide.title === 'string' &&
					typeof slide.subtitle === 'string' &&
					typeof slide.notes === 'string' &&
					Array.isArray(slide.points) &&
					slide.points.every((point) => typeof point === 'string'),
			) &&
			Array.isArray(project.messages) &&
			project.messages.every(
				(message) =>
					message &&
					typeof message.text === 'string' &&
					typeof message.id === 'string' &&
					['user', 'assistant'].includes(message.role),
			) &&
			Array.isArray(project.versions)
		)
			return {
				...project,
				id: project.id || uid(),
				phase: project.artifact ? 'ready' : 'outline',
			};
	} catch {
		/* Starting a draft also works when storage is unavailable. */
	}
	return initialProject();
}
export function saveProject(project: Project) {
	localStorage.setItem(STORAGE_KEY, JSON.stringify(project));
}
export function downloadFile(name: string, content: string, type: string) {
	const url = URL.createObjectURL(new Blob([content], { type }));
	const link = document.createElement('a');
	link.href = url;
	link.download = name;
	link.click();
	setTimeout(() => URL.revokeObjectURL(url), 1000);
}
