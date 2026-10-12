import type {
	ExecutionEvent,
	ExecutionNode,
	ExecutionPage,
	ExecutionPhase,
	ExecutionStatus,
	StudioJob,
} from './api';

export const executionPhases: { id: ExecutionPhase; label: string }[] = [
	{ id: 'context', label: '读取材料' },
	{ id: 'plan', label: '内容策划' },
	{ id: 'assets', label: '准备素材' },
	{ id: 'pages', label: '逐页创作' },
	{ id: 'validate', label: '检查修复' },
	{ id: 'export', label: '渲染导出' },
	{ id: 'visual', label: 'AI 视觉审稿' },
	{ id: 'done', label: '交付' },
];
export type DisplayStatus = ExecutionStatus | 'unknown';
export type DisplayNode = Omit<ExecutionNode, 'status'> & {
	id: ExecutionPhase;
	status: DisplayStatus;
};
export const executionStatusLabels: Record<DisplayStatus, string> = {
	pending: '待开始',
	running: '进行中',
	completed: '已完成',
	failed: '失败',
	cancelled: '已停止',
	unknown: '未知',
};
function phaseOf(value: unknown): ExecutionPhase | null {
	if (typeof value !== 'string') return null;
	const aliases: Record<string, ExecutionPhase> = {
		page: 'pages',
		repair: 'validate',
		quality: 'validate',
		planning: 'plan',
		reading: 'context',
	};
	return executionPhases.find((phase) => phase.id === value)?.id ?? aliases[value] ?? null;
}
const count = (value: unknown) =>
	typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
const validStatus = (status: unknown): status is ExecutionStatus =>
	typeof status === 'string' &&
	['pending', 'running', 'completed', 'failed', 'cancelled'].includes(status);

export function executionView(job: StudioJob) {
	const execution = job.execution;
	const checkpoint = typeof job.checkpoint === 'object' && job.checkpoint ? job.checkpoint : null;
	const source = execution ? 'execution' : 'legacy';
	let phase =
		phaseOf(execution?.phase) ??
		phaseOf(execution?.nodes?.find((node) => node.status === 'running')?.id);
	let inferredPhase = false;
	if (!phase)
		phase =
			phaseOf(checkpoint?.stage) ??
			phaseOf(job.events?.findLast((event) => phaseOf(event.phase))?.phase);
	if (!phase && job.status === 'completed') phase = 'done';
	if (!phase && job.stage > 0 && job.stage < 100) {
		phase =
			job.stage >= 75
				? 'export'
				: job.stage >= 60
					? 'validate'
					: job.stage >= 20
						? 'pages'
						: job.stage >= 10
							? 'plan'
							: 'context';
		inferredPhase = true;
	}
	const nodes: DisplayNode[] = executionPhases.map((item) => {
		const supplied = execution?.nodes?.findLast((node) => node.id === item.id);
		if (supplied)
			return {
				...item,
				...supplied,
				id: item.id,
				status: validStatus(supplied.status) ? supplied.status : 'unknown',
			};
		let status: DisplayStatus = 'unknown';
		if (!execution && phase === item.id && !inferredPhase) {
			if (job.status === 'running') status = 'running';
			if (job.status === 'failed') status = 'failed';
			if (job.status === 'cancelled') status = 'cancelled';
			if (job.status === 'completed' && item.id === 'done') status = 'completed';
		}
		return { ...item, status };
	});
	const byPath = new Map<string, ExecutionPage>();
	for (const page of execution?.pages ?? []) {
		if (!page.path || !Number.isInteger(page.index) || !validStatus(page.status)) continue;
		const previous = byPath.get(page.path);
		if (
			previous?.updatedAt !== undefined &&
			page.updatedAt !== undefined &&
			previous.updatedAt > page.updatedAt
		)
			continue;
		byPath.set(page.path, page);
	}
	const pages = [...byPath.values()].sort((a, b) => a.index - b.index);
	const saved =
		job.kind === 'render'
			? null
			: (count(checkpoint?.completedPages) ??
				(pages.length ? pages.filter((page) => page.status === 'completed').length : null));
	// An event feed can contain only the pages seen so far. Its array length does
	// not establish the planned total.
	const total = count(checkpoint?.totalPages) ?? count(job.result?.project?.pptd?.pageCount);
	const allEvents: ExecutionEvent[] = (
		execution?.events ??
		job.events?.map((event) => ({
			timestamp: event.timestamp,
			phase: phaseOf(event.phase) ?? undefined,
			status: typeof event.status === 'string' ? event.status : undefined,
			message: event.message ?? '',
		})) ??
		[]
	).filter((event) => typeof event.message === 'string' && event.message.trim());
	const repairPaths = [
		...new Set(
			allEvents
				.filter((event) => event.phase === 'validate' && event.pagePath)
				.map((event) => event.pagePath!),
		),
	];
	const events = allEvents.slice(-40);
	const visibleNodes =
		job.kind === 'render'
			? nodes.filter((node) => {
					if (node.id === 'export' || node.id === 'done') return true;
					const supplied = execution?.nodes?.findLast((item) => item.id === node.id);
					// A render can fail before its first export event (for example, reading
					// the source document). Keep that recorded failure visible.
					if (supplied?.status === 'failed' || supplied?.status === 'cancelled')
						return true;
					return (
						['validate', 'visual'].includes(node.id) &&
						(execution?.phase === node.id ||
							supplied?.status === 'running' ||
							supplied?.status === 'completed' ||
							allEvents.some((event) => phaseOf(event.phase) === node.id))
					);
				})
			: nodes;
	const visualPages = (execution?.visualPages ?? [])
		.filter((page) => page.path && Number.isInteger(page.index) && validStatus(page.status))
		.toSorted((a, b) => a.index - b.index);
	const suppliedReport = job.result?.project?.generationMeta?.validation?.visualReview;
	// A saved deck or a completed export alone cannot establish visual approval.
	const visualReport =
		suppliedReport &&
		suppliedReport.status === 'passed' &&
		(count(suppliedReport.totalPages) ?? 0) > 0 &&
		suppliedReport.reviewedPages === suppliedReport.totalPages &&
		suppliedReport.issueCount === 0
			? suppliedReport
			: null;
	return {
		source,
		phase,
		inferredPhase,
		nodes: visibleNodes,
		pages,
		visualPages,
		visual: execution?.visual ?? null,
		visualReport,
		events,
		repairPaths,
		saved,
		total,
	};
}
