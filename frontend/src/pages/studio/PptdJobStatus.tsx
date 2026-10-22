import {
	Check,
	ChevronDown,
	ChevronUp,
	Circle,
	CircleAlert,
	CircleHelp,
	Clock3,
	LoaderCircle,
	RotateCcw,
	Square,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import type { ExecutionPhase, StudioJob } from './api';
import { executionStatusLabels, executionView } from './executionState';
import type { DisplayStatus } from './executionState';
import { isRunningJob } from './pptdState';

function duration(seconds: number) {
	const total = Math.max(0, Math.floor(seconds));
	const minutes = Math.floor(total / 60);
	if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
	return minutes ? `${minutes} 分 ${total % 60} 秒` : `${total} 秒`;
}
function eventTime(timestamp?: number | string) {
	if (timestamp === undefined) return '';
	const date = new Date(typeof timestamp === 'number' ? timestamp * 1000 : timestamp);
	return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN');
}
function StatusIcon({ status }: { status: DisplayStatus }) {
	if (status === 'completed') return <Check size={13} />;
	if (status === 'running') return <LoaderCircle size={13} className="pptd-spin" />;
	if (status === 'failed') return <CircleAlert size={13} />;
	if (status === 'cancelled') return <Square size={11} />;
	if (status === 'unknown') return <CircleHelp size={13} />;
	return <Circle size={11} />;
}

export function PptdJobStatus({
	job,
	busy,
	onStop,
	onResume,
	onRetry,
}: {
	job: StudioJob;
	busy: boolean;
	onStop: () => void;
	onResume: () => void;
	onRetry: () => void;
}) {
	const running = isRunningJob(job);
	const failed = ['failed', 'cancelled', 'interrupted'].includes(job.status);
	const renderJob = job.kind === 'render';
	const execution = executionView(job);
	const [now, setNow] = useState(() => Date.now() / 1000);
	const [expanded, setExpanded] = useState(false);
	const [issuesExpanded, setIssuesExpanded] = useState(false);
	const [collapsed, setCollapsed] = useState(job.status === 'completed');
	const [selectedPhase, setSelectedPhase] = useState<ExecutionPhase | null>(null);
	const detailsRef = useRef<HTMLDivElement>(null);
	const panelRef = useRef<HTMLElement>(null);
	const toggleRef = useRef<HTMLButtonElement>(null);
	const selected =
		execution.nodes.find((node) => node.id === (selectedPhase ?? execution.phase)) ??
		execution.nodes[0];
	const current = execution.nodes.find((node) => node.id === execution.phase);
	const repairPaths = new Set(execution.repairPaths);
	const issues = job.diagnostic?.issues ?? [];
	const warningIssues = job.result?.warnings ?? [];
	const completedWithWarnings = job.status === 'completed' && warningIssues.length > 0;
	const displayedPages =
		selected.id === 'visual'
			? execution.visualPages
			: selected.id === 'validate'
				? execution.pages.filter(
						(page) =>
							repairPaths.has(page.path) ||
							(execution.phase === 'validate' &&
								['running', 'failed'].includes(page.status)),
					)
				: execution.pages;
	const activePage =
		displayedPages.find((page) => page.status === 'failed') ??
		displayedPages.find((page) => page.status === 'running');
	useEffect(() => {
		if (!running) return;
		const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
		return () => clearInterval(timer);
	}, [running]);
	useEffect(() => {
		if (job.status === 'completed') {
			const focused = window.document.activeElement;
			if (
				focused instanceof HTMLElement &&
				panelRef.current?.contains(focused) &&
				focused.closest('.pptd-execution-timeline, .pptd-execution-details')
			)
				toggleRef.current?.focus();
			setExpanded(false);
			setCollapsed(true);
		}
	}, [job.status]);
	useEffect(() => {
		if (!expanded || !['pages', 'validate', 'visual'].includes(selected.id)) return;
		const container = detailsRef.current;
		const active = container?.querySelector<HTMLElement>('[data-active="true"]');
		if (!container || !active) return;
		const bounds = container.getBoundingClientRect(),
			row = active.getBoundingClientRect();
		if (row.bottom > bounds.bottom) container.scrollTop += row.bottom - bounds.bottom;
		else if (row.top < bounds.top) container.scrollTop -= bounds.top - row.top;
	}, [expanded, selected.id, activePage?.path, activePage?.status, activePage?.attempt]);
	const elapsed =
		typeof job.createdAt === 'number'
			? duration((running ? now : (job.updatedAt ?? now)) - job.createdAt)
			: null;
	const latest =
		typeof job.updatedAt === 'number'
			? running
				? `${duration(now - job.updatedAt)}前`
				: eventTime(job.updatedAt)
			: null;
	const labels = {
		queued: '等待开始',
		running:
			execution.phase === 'visual'
				? renderJob
					? '导出前视觉审稿中'
					: 'AI 审稿中'
				: renderJob
					? '导出前检查中'
					: '正在创作',
		completed: completedWithWarnings ? `完成 · ${warningIssues.length} 项待确认` : '生成完成',
		failed: renderJob ? '导出前检查失败' : '生成失败',
		cancelled: '任务已停止',
		interrupted: '任务已中断',
	};
	const recentEvents = execution.events.slice(-12).reverse();
	const selectedEvents = execution.events
		.filter((event) => event.phase === selected.id)
		.slice(-8)
		.reverse();
	const summary =
		selected.summary ||
		(selected.id === execution.phase
			? job.message
			: selected.status === 'unknown'
				? '没有可确认的阶段记录。'
				: selected.status === 'pending'
					? '等待开始此阶段。'
					: executionStatusLabels[selected.status]);
	function selectStage(phase: ExecutionPhase) {
		setIssuesExpanded(false);
		setSelectedPhase(phase === execution.phase ? null : phase);
		setExpanded(true);
		setCollapsed(false);
	}
	function toggleDetails() {
		setIssuesExpanded(false);
		if (job.status === 'completed') {
			setCollapsed(!collapsed);
			setExpanded(false);
		} else {
			setSelectedPhase(null);
			setExpanded(!expanded);
		}
	}
	return (
		<section
			ref={panelRef}
			className={`pptd-live-status ${failed ? 'has-failure' : ''} ${expanded ? 'is-expanded' : ''} ${collapsed ? 'is-collapsed' : ''}`}
			aria-label="当前任务状态"
		>
			<div className="pptd-live-heading">
				<strong>
					{running ? (
						<LoaderCircle size={16} className="pptd-spin" />
					) : failed ? (
						<CircleAlert size={16} />
					) : (
						<Check size={16} />
					)}
					创作过程 <span className="pptd-execution-badge">{labels[job.status]}</span>
				</strong>
				<div>
					{execution.saved !== null && (
						<span className="pptd-live-page-count">
							已保存 {execution.saved}
							{execution.total !== null ? `/${execution.total}` : ''} 页
						</span>
					)}
					<button
						ref={toggleRef}
						type="button"
						className="pptd-execution-toggle"
						aria-expanded={job.status === 'completed' ? !collapsed : expanded}
						onClick={toggleDetails}
					>
						{(job.status === 'completed' ? !collapsed : expanded) ? (
							<ChevronUp size={14} />
						) : (
							<ChevronDown size={14} />
						)}
						{job.status === 'completed'
							? collapsed
								? '展开创作过程'
								: '收起创作过程'
							: expanded
								? '收起详情'
								: '查看详情'}
					</button>
				</div>
			</div>
			<div className="pptd-live-overview">
				<p className="pptd-live-message" aria-live="polite" title={job.message}>
					{job.message}
				</p>
				{renderJob && (
					<small>导出前验证：排版与 AI 视觉审稿通过后才会生成 PPTX/PDF。</small>
				)}
				{failed && issues.length > 0 && (
					<button
						type="button"
						className="pptd-diagnostic-toggle"
						aria-expanded={issuesExpanded}
						onClick={() => {
							setIssuesExpanded(!issuesExpanded);
							setExpanded(false);
						}}
					>
						{issuesExpanded ? '收起具体问题' : '查看具体问题'}（{issues.length}项）
					</button>
				)}
			</div>
			{execution.visualReport && (
				<p className="pptd-visual-result" role="status" aria-label="AI 视觉审稿结果">
					<Check size={12} />
					<span>
						AI 视觉审稿通过 · {execution.visualReport.reviewedPages}/
						{execution.visualReport.totalPages} 页
						{execution.visualReport.rounds > 0
							? ` · ${execution.visualReport.rounds} 轮审稿`
							: ''}
						{(execution.visualReport.repairedIssueCount ?? 0) > 0
							? ` · 自动修复 ${execution.visualReport.repairedIssueCount} 项`
							: ''}
					</span>
				</p>
			)}
			{completedWithWarnings && (
				<details className="pptd-export-warnings">
					<summary>排版待确认 · {warningIssues.length} 项</summary>
					<div>
						<ol className="pptd-diagnostic-issues">
							{warningIssues.map((issue, index) => (
								<li key={`${issue.location}-${issue.code}-${index}`}>
									<p>{issue.message}</p>
									<small>
										{issue.location ? `位置：${issue.location}` : ''}
										{issue.code ? ` · ${issue.code}` : ''}
									</small>
								</li>
							))}
						</ol>
					</div>
				</details>
			)}
			{failed && job.error && job.error !== job.message && (
				<p className="pptd-live-error" title={job.error}>
					{job.error}
				</p>
			)}
			{!collapsed && (
				<ol
					className="pptd-execution-timeline"
					aria-label="创作阶段"
					style={{
						gridTemplateColumns: `repeat(${execution.nodes.length}, minmax(0, 1fr))`,
					}}
				>
					{execution.nodes.map((node) => (
						<li key={node.id}>
							<button
								type="button"
								className={`pptd-execution-node is-${node.status} ${execution.phase === node.id ? 'is-current' : ''} ${expanded && selected.id === node.id ? 'is-selected' : ''}`}
								aria-label={`${node.label}：${executionStatusLabels[node.status]}${execution.inferredPhase && execution.phase === node.id ? '，推测当前阶段' : ''}`}
								aria-current={execution.phase === node.id ? 'step' : undefined}
								aria-expanded={expanded && selected.id === node.id}
								onClick={() => selectStage(node.id)}
							>
								<span className="pptd-execution-node-icon">
									<StatusIcon status={node.status} />
								</span>
								<strong>
									{node.id === 'visual' ? (
										<>
											<span className="pptd-visual-prefix">AI </span>视觉审稿
										</>
									) : (
										node.label
									)}
								</strong>
								<small>
									{execution.inferredPhase && execution.phase === node.id
										? '推测当前'
										: executionStatusLabels[node.status]}
								</small>
							</button>
						</li>
					))}
				</ol>
			)}
			{execution.source === 'legacy' && !collapsed && (
				<p className="pptd-execution-legacy">
					旧任务未记录完整过程，未知节点不代表已完成。
					{execution.inferredPhase && current
						? `当前阶段按旧进度推测为“${current.label}”。`
						: ''}
				</p>
			)}
			{expanded && !collapsed && (
				<div
					className="pptd-execution-details"
					ref={detailsRef}
					role="region"
					aria-label={`${selected.label}详情`}
				>
					<div className="pptd-execution-detail-heading">
						<strong>{selected.label}</strong>
						<span>
							{executionStatusLabels[selected.status]}
							{selected.startedAt ? ` · ${eventTime(selected.startedAt)} 开始` : ''}
							{selected.status !== 'running' && selected.startedAt && selected.endedAt
								? ` · 用时 ${duration(selected.endedAt - selected.startedAt)}`
								: ''}
							{typeof selected.progress === 'number'
								? ` · 实际修复进度 ${selected.progress}%`
								: ''}
						</span>
					</div>
					<p className="pptd-execution-summary">{summary}</p>
					{selected.id === 'visual' && execution.visual && (
						<div className="pptd-visual-coverage" aria-live="polite">
							<strong>
								{execution.visual.attempt
									? `第 ${execution.visual.attempt} 轮 · `
									: ''}
								{typeof execution.visual.covered === 'number' &&
								typeof execution.visual.total === 'number'
									? `已审阅 ${execution.visual.covered}/${execution.visual.total} 页`
									: '正在检查整稿视觉'}
							</strong>
							<span>AI 自动检查、修复并复审</span>
						</div>
					)}
					{failed &&
						selected.id === execution.phase &&
						job.error &&
						job.error !== summary && (
							<p className="pptd-execution-diagnostic">{job.error}</p>
						)}
					{['pages', 'validate', 'visual'].includes(selected.id) &&
						(displayedPages.length ? (
							<ol
								className="pptd-execution-pages"
								aria-label={
									selected.id === 'visual'
										? '视觉审稿页面状态'
										: selected.id === 'validate'
											? '修复页面状态'
											: '逐页状态'
								}
							>
								{displayedPages.map((page) => (
									<li
										key={page.path}
										className={`pptd-execution-page is-${page.status}`}
										data-page-path={page.path}
										data-active={
											activePage?.path === page.path ? 'true' : undefined
										}
									>
										<div>
											<StatusIcon status={page.status} />
											<strong>
												第 {page.index + 1} 页 ·{' '}
												{page.title || '未命名页面'}
											</strong>
											<span>{executionStatusLabels[page.status]}</span>
										</div>
										{page.attempt && page.attempt > 1 ? (
											<small>
												{selected.id === 'visual'
													? `第 ${page.attempt} 轮审稿`
													: `第 ${page.attempt} 次尝试`}
											</small>
										) : null}
										{selected.id === 'visual' &&
											typeof page.issueCount === 'number' &&
											page.issueCount > 0 && (
												<small>
													{page.issueCount} 项问题 · AI 自动处理
												</small>
											)}
										{page.message && <p>{page.message}</p>}
									</li>
								))}
							</ol>
						) : (
							<p className="pptd-execution-unknown">
								{selected.id === 'visual'
									? '正在检查整稿，逐页审阅结果将在此显示。'
									: selected.id === 'validate'
										? '尚未记录需要逐页修复的问题。'
										: '此任务没有逐页记录，无法确认每一页的状态。'}
							</p>
						))}
					{selected.id !== 'pages' && !!selectedEvents.length && (
						<ol className="pptd-execution-stage-events">
							{selectedEvents.map((event, index) => (
								<li key={`${event.timestamp}-${index}`}>
									<time>{eventTime(event.timestamp)}</time>
									<span>{event.message}</span>
								</li>
							))}
						</ol>
					)}
					<details className="pptd-execution-events">
						<summary>
							最近事件{recentEvents.length ? ` · ${recentEvents.length}` : ''}
						</summary>
						{recentEvents.length ? (
							<ol>
								{recentEvents.map((event, index) => (
									<li key={`${event.timestamp}-${index}`}>
										<time>{eventTime(event.timestamp)}</time>
										<span>
											{event.message}
											{event.attempt && event.attempt > 1
												? `（第 ${event.attempt} 次尝试）`
												: ''}
										</span>
									</li>
								))}
							</ol>
						) : (
							<p>暂无公开执行事件。</p>
						)}
					</details>
				</div>
			)}
			{failed && issuesExpanded && issues.length > 0 && (
				<div
					className="pptd-execution-details pptd-diagnostic-details"
					role="region"
					aria-label="具体问题"
				>
					<ol className="pptd-diagnostic-issues">
						{issues.map((issue, index) => {
							const matchedPage = execution.pages.find(
								(page) => page.path === issue.page,
							);
							const pageIndex = issue.pageIndex ?? matchedPage?.index;
							const title = issue.pageTitle || matchedPage?.title;
							return (
								<li key={`${issue.location}-${issue.code}-${index}`}>
									{typeof pageIndex === 'number' && pageIndex >= 0 && (
										<strong>
											第 {pageIndex + 1} 页{title ? ` · ${title}` : ''}
										</strong>
									)}
									<p>{issue.message}</p>
									<small>
										具体位置：{issue.location}
										{issue.elementId ? ` · 元素：${issue.elementId}` : ''}
										{issue.code ? ` · ${issue.code}` : ''}
									</small>
								</li>
							);
						})}
					</ol>
				</div>
			)}
			<div className="pptd-execution-footer">
				<div className="pptd-live-timing">
					{elapsed && (
						<span>
							<Clock3 size={11} />
							已用 {elapsed}
						</span>
					)}
					{latest && (
						<span>
							{running ? '最近状态更新' : '最后更新'} {latest}
						</span>
					)}
					{running && <span className="pptd-live-explanation">耗时不代表完成进度</span>}
				</div>
				<div className="pptd-live-actions">
					{running && (
						<button className="pptd-secondary" onClick={onStop}>
							<Square size={12} />
							停止任务
						</button>
					)}
					{failed && job.recoverable && job.checkpoint && (
						<button className="pptd-primary" disabled={busy} onClick={onResume}>
							<RotateCcw size={14} />
							从检查点继续
						</button>
					)}
					{failed && job.recoverable !== false && !job.checkpoint && (
						<button className="pptd-secondary" disabled={busy} onClick={onRetry}>
							重新生成
						</button>
					)}
				</div>
			</div>
		</section>
	);
}
