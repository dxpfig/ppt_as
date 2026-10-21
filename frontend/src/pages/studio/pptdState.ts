import type { JobRequest, StudioJob, Workflow, DesignMode, Scenario } from './api';
import type { Project, PptdPage } from './model';

export type GenerationPreferences = {
	pageCount: string;
	audience: string;
	workflow: Workflow;
	designMode: DesignMode;
	usage: 'reading' | 'presentation';
	scenario: Scenario;
	allowResearch: boolean;
	speakerNotes: boolean;
	animationRequested: boolean;
	designSystem: string;
	references: string;
};
export const defaultPreferences: GenerationPreferences = {
	pageCount: 'auto',
	audience: '团队分享',
	workflow: 'new',
	designMode: 'autonomous',
	usage: 'presentation',
	scenario: 'auto',
	allowResearch: false,
	speakerNotes: false,
	animationRequested: false,
	designSystem: '',
	references: '',
};
export function generationOptions(
	preferences: Partial<GenerationPreferences>,
): Omit<JobRequest, 'kind' | 'project'> {
	const values = { ...defaultPreferences, ...preferences };
	const pageCount = values.pageCount === 'auto' ? null : Number(values.pageCount);
	if (pageCount !== null && (!Number.isInteger(pageCount) || pageCount < 1 || pageCount > 60)) {
		throw new Error('页数请输入 1–60，或选择自动。');
	}
	return { ...values, pageCount };
}
export const isRunningJob = (job: Pick<StudioJob, 'status'> | null) =>
	!!job && ['queued', 'running'].includes(job.status);
export const isLegacyProject = (project: Project) => !project.pptd && project.slides.length > 0;

export type ImportWarningGroup = {
	message: string;
	count: number;
};

// The importer reports one issue per affected shape. Users need the distinct
// capability limits and their scope, not the same sentence dozens of times.
export function groupImportWarnings(warnings: string[] | undefined): ImportWarningGroup[] {
	const groups = new Map<string, ImportWarningGroup>();
	for (const warning of warnings ?? []) {
		const message = warning.trim();
		if (!message) continue;
		const existing = groups.get(message);
		if (existing) existing.count += 1;
		else groups.set(message, { message, count: 1 });
	}
	return [...groups.values()];
}

// Navigation previews use the current source even before an export passes review.
export function pagePreviewSources(
	project: Project,
	pagePath: string,
	index: number,
	origin: string,
): { image?: string; view: string } {
	if (!project.pptd) return { view: '' };
	const view = new URL(project.pptd.viewUrl, origin);
	view.searchParams.set('embed', '1');
	view.searchParams.set('mode', 'preview');
	view.searchParams.set('thumbnail', '1');
	view.searchParams.set('page', pagePath);
	view.searchParams.set('revision', String(project.pptd.revision));
	const artifactRevision = project.artifact?.pptdRevision ?? project.artifact?.revision;
	return {
		image:
			artifactRevision === project.pptd.revision
				? project.artifact?.previewUrls[index]
				: undefined,
		view: view.href,
	};
}

// A document edit invalidates exports from the previous revision, even if an
// in-flight metadata response still contains their links.
export function acceptServerProject(previous: Project, incoming: Project): Project {
	const next = {
		...incoming,
		slides: incoming.slides ?? [],
		messages: incoming.messages ?? [],
		versions: incoming.versions ?? [],
	};
	const revision = next.pptd?.revision;
	const artifactRevision = next.artifact?.pptdRevision ?? next.artifact?.revision;
	if (
		revision !== undefined &&
		next.artifact &&
		((artifactRevision !== undefined && artifactRevision !== revision) ||
			(previous.id === next.id &&
				previous.pptd?.revision !== revision &&
				previous.artifact?.id === next.artifact.id))
	)
		next.artifact = undefined;
	return next;
}
function plainTitle(value: unknown): string {
	if (typeof value !== 'string') return '';
	// This is plain-text extraction only. The result is always rendered as React
	// text, never injected as HTML or attached to a live document.
	const entities: Record<string, string> = {
		amp: '&',
		lt: '<',
		gt: '>',
		quot: '"',
		apos: "'",
		nbsp: ' ',
		ndash: '–',
		mdash: '—',
		hellip: '…',
	};
	return value
		.replace(/<(script|style|iframe|object|svg|math)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '')
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/<(?:br\s*\/?|\/(?:p|div|h[1-6]|li))\s*>/gi, ' ')
		.replace(/<\/?[a-z][^>]*>/gi, '')
		.replace(/&(#x[\da-f]+|#\d+|[a-z]+);/gi, (match, entity: string) => {
			if (!entity.startsWith('#')) return entities[entity.toLowerCase()] ?? match;
			const hex = entity[1].toLowerCase() === 'x';
			const code = Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10);
			return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff)
				? String.fromCodePoint(code)
				: match;
		})
		.replace(/\s+/g, ' ')
		.trim()
		.slice(0, 80);
}
export function pageTitle(page: PptdPage, index: number): string {
	for (const key of ['title', 'name', 'label']) {
		const label = plainTitle(page.data[key]);
		if (label) return label;
	}
	const elements = page.data.elements;
	if (Array.isArray(elements)) {
		const candidates = elements
			.filter(
				(element: unknown): element is Record<string, unknown> =>
					typeof element === 'object' &&
					element !== null &&
					'elementType' in element &&
					element.elementType === 'text',
			)
			.map((element) => {
				const content = element.content;
				const title =
					content && typeof content === 'object' && 'text' in content
						? plainTitle(content.text)
						: '';
				const id = String(element.elementId || '').toLowerCase();
				const priority =
					id === 'title'
						? 0
						: /(?:^|[-_\s])(?:title|heading)(?:$|[-_\s])/.test(id)
							? 1
							: 2;
				return { title, priority };
			})
			.filter((element) => element.title)
			.sort((a, b) => a.priority - b.priority);
		if (candidates[0]) return candidates[0].title;
	}
	return `第 ${index + 1} 页`;
}
