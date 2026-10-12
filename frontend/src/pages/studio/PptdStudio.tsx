import {
	ArrowUpRight,
	ChevronLeft,
	Check,
	ChevronRight,
	Download,
	FileArchive,
	FileText,
	History,
	Layers,
	LoaderCircle,
	Maximize,
	Minimize,
	PanelLeftClose,
	PanelLeftOpen,
	Pencil,
	Plus,
	RefreshCw,
	RotateCcw,
	Sparkles,
	Square,
	Upload,
	X,
} from 'lucide-react';
import { useTheme } from 'next-themes';
import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';

import type { DesignMode, Scenario, Workflow } from './api';
import { studioApi } from './api';
import { ArtifactPreview } from './ArtifactPreview';
import { PptdJobStatus } from './PptdJobStatus';
import {
	defaultPreferences,
	generationOptions,
	isLegacyProject,
	pageTitle,
	pagePreviewSources,
} from './pptdState';
import type { GenerationPreferences } from './pptdState';
import { PptdThumbnail } from './PptdThumbnail';
import { SlideDraft } from './SlideDraft';
import { ThemeSwitcher } from './ThemeSwitcher';
import { usePptdStudio } from './usePptdStudio';
import { useMediaQuery, usePageWheel, usePresentation } from './usePreviewNavigation';
import { WorkHistory } from './WorkHistory';
import './studio.css';
import './pptd-studio.css';
import './studio-theme.css';

const scenarios: { value: Scenario; label: string }[] = [
	{ value: 'auto', label: '自动识别场景' },
	{ value: 'management-report', label: '工作汇报' },
	{ value: 'analysis-decision', label: '分析决策' },
	{ value: 'business-plan', label: '商业计划' },
	{ value: 'tech-engineering', label: '技术工程' },
	{ value: 'academic-research', label: '学术研究' },
	{ value: 'education-training', label: '教育培训' },
	{ value: 'brand-creative', label: '品牌创意' },
];
const statusLabels = {
	queued: '等待开始',
	running: '正在创作',
	completed: '已完成',
	failed: '任务失败',
	cancelled: '已停止',
	interrupted: '任务中断',
};

export function PptdStudio() {
	const studio = usePptdStudio();
	return <PptdWorkspace key={studio.project.id} studio={studio} />;
}
function PptdWorkspace({ studio }: { studio: ReturnType<typeof usePptdStudio> }) {
	const { resolvedTheme } = useTheme();
	const { project, document, health, job, busy, connected } = studio;
	const [prompt, setPrompt] = useState('');
	const [preferences, setPreferences] = useState<GenerationPreferences>({
		...defaultPreferences,
		audience: project.audience || defaultPreferences.audience,
		references: project.references || '',
		workflow: project.pptd ? 'edit' : 'new',
	});
	const [templateId, setTemplateId] = useState(project.templateId || '');
	const workflowSelectedByUser = useRef(false);
	const hasDocument = !!project.pptd;
	useEffect(() => {
		if (hasDocument && !workflowSelectedByUser.current) {
			setPreferences((previous) =>
				previous.workflow === 'new' ? { ...previous, workflow: 'edit' } : previous,
			);
		}
	}, [hasDocument]);
	const [images, setImages] = useState<{ name: string; imageData: string; size: number }[]>([]);
	const [readingImages, setReadingImages] = useState(false);
	const [selectedPage, setSelectedPage] = useState('');
	const [legacyIndex, setLegacyIndex] = useState(0);
	const [mode, setMode] = useState<'preview' | 'edit'>('preview');
	const [showVersions, setShowVersions] = useState(false);
	const [scope, setScope] = useState<'page' | 'deck'>('page');
	const [notice, setNotice] = useState('');
	const [deleteId, setDeleteId] = useState<string | null>(null);
	useEffect(() => {
		if (job?.id && window.matchMedia('(max-width: 760px)').matches) {
			window.document.querySelector('.pptd-live-status')?.scrollIntoView({ block: 'start' });
		}
	}, [job?.id]);
	const iframeRef = useRef<HTMLIFrameElement>(null);
	const workbenchRef = useRef<HTMLElement>(null);
	const pageListRef = useRef<HTMLElement>(null);
	const compactHistory = useMediaQuery('(min-width: 761px) and (max-width: 1440px)');
	const narrow = useMediaQuery('(max-width: 1199px)');
	const [briefPreference, setBriefPreference] = useState<boolean | null>(null);
	const briefOpen = briefPreference ?? (!narrow || !hasDocument);
	const presentation = usePresentation(workbenchRef, iframeRef, project.id);
	const inputRef = useRef<HTMLTextAreaElement>(null);
	const templateInput = useRef<HTMLInputElement>(null);
	const referenceInput = useRef<HTMLInputElement>(null);
	const imageInput = useRef<HTMLInputElement>(null);
	const legacy = isLegacyProject(project);
	const pages = document?.pages || [];
	const currentPage = pages.find((page) => page.path === selectedPage) ?? pages[0];
	const pageIndex = currentPage ? pages.indexOf(currentPage) : 0;
	const versions = document?.versions ?? project.pptd?.versions ?? [];
	const template = studio.templates.find((item) => item.id === templateId);
	const canGenerate = !busy && !readingImages && connected && !!health?.llmConfigured && !legacy;
	const terminal = job && ['failed', 'cancelled', 'interrupted'].includes(job.status);
	const checkpoint =
		typeof job?.checkpoint === 'string' ? job.checkpoint : job?.checkpoint?.summary;
	const completedPages =
		typeof job?.checkpoint === 'object' && typeof job.checkpoint.completedPages === 'number'
			? job.checkpoint.completedPages
			: undefined;
	const iframeUrl = project.pptd
		? (() => {
				const url = new URL(project.pptd.viewUrl, window.location.origin);
				url.searchParams.set('embed', '1');
				url.searchParams.set('mode', mode);
				url.searchParams.set('revision', String(project.pptd.revision));
				return url.pathname + url.search;
			})()
		: '';
	function selectPage(path: string) {
		setSelectedPage(path);
		iframeRef.current?.contentWindow?.postMessage(
			{ type: 'pptd:select-page', projectId: project.id, page: path },
			window.location.origin,
		);
	}
	function syncIframe() {
		iframeRef.current?.contentWindow?.postMessage(
			{
				type: 'pptd:ui-theme',
				projectId: project.id,
				theme: resolvedTheme === 'dark' ? 'dark' : 'light',
			},
			window.location.origin,
		);
		presentation.sync();
		if (currentPage)
			iframeRef.current?.contentWindow?.postMessage(
				{ type: 'pptd:select-page', projectId: project.id, page: currentPage.path },
				window.location.origin,
			);
	}
	useEffect(() => {
		iframeRef.current?.contentWindow?.postMessage(
			{
				type: 'pptd:ui-theme',
				projectId: project.id,
				theme: resolvedTheme === 'dark' ? 'dark' : 'light',
			},
			window.location.origin,
		);
	}, [resolvedTheme, project.id]);
	const currentPagePath = currentPage?.path;
	usePageWheel(
		pageListRef,
		(direction) => {
			const page = pages[Math.max(0, Math.min(pages.length - 1, pageIndex + direction))];
			if (page) selectPage(page.path);
		},
		!!pages.length && !busy,
	);
	useEffect(() => {
		const list = pageListRef.current;
		const selected = list?.querySelector<HTMLElement>('[aria-current="page"]');
		if (!list || !selected) return;
		const item = selected.getBoundingClientRect();
		const bounds = list.getBoundingClientRect();
		if (item.top < bounds.top) list.scrollTop -= bounds.top - item.top;
		else if (item.bottom > bounds.bottom) list.scrollTop += item.bottom - bounds.bottom;
	}, [currentPagePath]);
	useEffect(() => {
		if (currentPagePath)
			iframeRef.current?.contentWindow?.postMessage(
				{ type: 'pptd:select-page', projectId: project.id, page: currentPagePath },
				window.location.origin,
			);
	}, [currentPagePath, project.id, iframeUrl]);
	useEffect(() => {
		const receive = (event: MessageEvent) => {
			if (
				event.origin !== window.location.origin ||
				event.source !== iframeRef.current?.contentWindow
			)
				return;
			const message = event.data as {
				type?: string;
				projectId?: string;
				page?: string;
			} | null;
			if (!message || (message.projectId && message.projectId !== project.id)) return;
			if (message.type === 'pptd:ready' && currentPage) {
				presentation.sync();
				iframeRef.current?.contentWindow?.postMessage(
					{ type: 'pptd:select-page', projectId: project.id, page: currentPage.path },
					window.location.origin,
				);
			}
			if (message.type === 'pptd:changed') {
				void studio
					.refreshProject()
					.then(() => setNotice('修改已保存，请重新导出以获取最新文件。'))
					.catch((cause: unknown) =>
						studio.setError(cause instanceof Error ? cause.message : '刷新文档失败。'),
					);
			}
			if (
				['pptd:select-page', 'pptd:page-selected', 'pptd:selected'].includes(
					message.type || '',
				) &&
				message.page
			)
				setSelectedPage(message.page);
		};
		window.addEventListener('message', receive);
		return () => window.removeEventListener('message', receive);
	}, [project.id, currentPage, studio, presentation]);
	useEffect(() => {
		if (!notice) return;
		const timer = setTimeout(() => setNotice(''), 5000);
		return () => clearTimeout(timer);
	}, [notice]);
	function update<K extends keyof GenerationPreferences>(
		key: K,
		value: GenerationPreferences[K],
	) {
		if (key === 'workflow') workflowSelectedByUser.current = true;
		setPreferences((previous) => ({ ...previous, [key]: value }));
	}
	/** Fill the spec textarea from a shipped preset; the user can still edit it. */
	async function loadDesignSystem(id: string) {
		if (!id) return;
		try {
			const spec = await studioApi.designSystem(id);
			update('designSystem', spec.text);
		} catch (cause) {
			studio.setError(
				cause instanceof Error ? cause.message : '无法读取该预设设计规范。',
			);
		}
	}
	async function submit(event: FormEvent) {
		event.preventDefault();
		if (!prompt.trim() || !canGenerate) return;
		try {
			const kind = project.pptd && preferences.workflow === 'edit' ? 'revise' : 'outline';
			const started = await studio.start(kind, {
				...generationOptions(preferences),
				prompt: prompt.trim(),
				attachments: images.map(({ name, imageData }) => ({ name, imageData })),
				templateId: preferences.designMode === 'template' ? templateId : undefined,
				selectedSlideId:
					kind === 'revise' && scope === 'page' ? currentPage?.path : undefined,
			});
			if (started) {
				setPrompt('');
				setPreferences((previous) => ({ ...previous, workflow: 'edit' }));
			}
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '请检查输入设置。');
		}
	}
	async function readReference(event: ChangeEvent<HTMLInputElement>) {
		const file = event.target.files?.[0];
		if (!file) return;
		try {
			if (file.size > 2 * 1024 * 1024) throw new Error('参考文本请控制在 2 MB 以内。');
			const text = await file.text();
			update('references', `${preferences.references}\n\n【${file.name}】\n${text}`.trim());
			setNotice(`已添加参考资料：${file.name}`);
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '无法读取资料。');
		}
		event.target.value = '';
	}
	async function uploadTemplate(event: ChangeEvent<HTMLInputElement>) {
		const file = event.target.files?.[0];
		if (file) {
			const uploaded = await studio.uploadTemplate(file);
			if (uploaded) setTemplateId(uploaded.id);
		}
		event.target.value = '';
	}
	async function readImages(event: ChangeEvent<HTMLInputElement>) {
		const files = Array.from(event.target.files || []);
		event.target.value = '';
		if (!files.length) return;
		setReadingImages(true);
		try {
			if (images.length + files.length > 6) throw new Error('最多添加 6 张参考图片。');
			if (
				images.reduce((sum, image) => sum + image.size, 0) +
					files.reduce((sum, file) => sum + file.size, 0) >
				10 * 1024 * 1024
			)
				throw new Error('参考图片总大小请控制在 10 MB 以内。');
			const added = await Promise.all(
				files.map(async (file) => {
					if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
						throw new Error('参考图片支持 PNG、JPEG 和 WebP。');
					const imageData = await new Promise<string>((resolve, reject) => {
						const reader = new FileReader();
						reader.onload = () => resolve(String(reader.result));
						reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
						reader.readAsDataURL(file);
					});
					return { name: file.name, imageData, size: file.size };
				}),
			);
			setImages((previous) => [...previous, ...added]);
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '无法读取图片。');
		} finally {
			setReadingImages(false);
		}
	}
	return (
		<div className="studio-app pptd-app">
			<header className="studio-header">
				<a className="studio-brand" href="/" aria-label="演示助手首页">
					<span className="brand-symbol">
						<Layers size={20} />
					</span>
					<strong>
						演示助手<span>STUDIO</span>
					</strong>
				</a>
				<span className="header-divider" />
				<div className="project-breadcrumb">
					<span>我的演示</span>
					<ChevronRight size={14} />
					<strong title={project.title}>{project.title}</strong>
				</div>
				<div className="header-actions">
					<span className="saved-label">
						<span />
						{busy
							? '正在处理'
							: project.pptd
								? `已保存 · 版本 ${project.pptd.revision}`
								: legacy
									? '旧版项目'
									: '新建演示'}
					</span>
					<ThemeSwitcher />
					<button className="new-chat" disabled={busy} onClick={studio.newProject}>
						<Plus size={14} />
						新建
					</button>
				</div>
			</header>
			<div
				className={`pptd-layout ${job ? 'has-job' : ''} ${project.pptd ? 'has-document' : ''} ${briefOpen ? 'has-brief' : ''}`}
			>
				<WorkHistory
					projects={studio.projects}
					current={project}
					compact={compactHistory}
					busy={busy}
					connected={connected}
					onOpen={(id) => void studio.openProject(id)}
					onNew={studio.newProject}
					onDelete={(item) => setDeleteId(item.id)}
					onRefresh={() => void studio.refreshProjects()}
					lastDeleted={studio.lastDeleted}
					onUndo={() => void studio.undoDelete()}
				/>
				<main className="pptd-brief" hidden={!briefOpen} id="pptd-creation-settings">
					<div className="pptd-section-title">
						<Sparkles size={17} />
						<strong>{project.pptd ? '继续打磨演示' : '从一个想法开始'}</strong>
						<button
							className="studio-icon"
							aria-label="收起创作设置"
							onClick={() => setBriefPreference(false)}
						>
							<PanelLeftClose size={16} />
						</button>
					</div>
					<div className="pptd-brief-scroll">
						<div className="pptd-intro">
							<h1>
								{project.pptd ? '让表达更进一步' : '把你的想法，变成精彩演示。'}
							</h1>
							<p>
								{project.pptd
									? '描述修改目标，或直接在右侧选择元素编辑。'
									: '输入主题、材料或完整大纲，自动规划内容和视觉设计。'}
							</p>
						</div>
						<div
							className={`pptd-model ${health?.llmConfigured ? '' : 'is-unavailable'}`}
						>
							<span className="pptd-status-dot" />
							<div>
								<strong>
									{connected ? health?.model || '读取模型配置…' : '服务未连接'}
								</strong>
								<small>
									{connected
										? `${health?.provider || '模型服务'} · ${health?.llmConfigured ? '已配置' : '未配置模型凭据'}`
										: '检查后端服务后重新连接'}
								</small>
							</div>
							<button
								className="studio-icon"
								title="刷新模型状态"
								aria-label="刷新模型状态"
								onClick={() => void studio.reconnect()}
							>
								<RefreshCw size={14} />
							</button>
						</div>
						{studio.error && (
							<div className="pptd-error" role="alert">
								<span>{studio.error}</span>
								<button
									className="studio-icon"
									aria-label="关闭错误提示"
									onClick={() => studio.setError('')}
								>
									<X size={14} />
								</button>
							</div>
						)}
						{notice && (
							<div className="pptd-notice" role="status">
								<Check size={14} />
								{notice}
							</div>
						)}
						<form onSubmit={(event) => void submit(event)}>
							<label className="pptd-field">
								<span>{project.pptd ? '修改要求' : '演示主题或内容'}</span>
								<textarea
									ref={inputRef}
									value={prompt}
									onChange={(event) => setPrompt(event.target.value)}
									disabled={busy || legacy}
									rows={5}
									placeholder={
										project.pptd
											? '例如：让这页突出核心结论，用图表解释数据变化…'
											: '例如：面向管理团队的季度业务复盘，重点呈现增长、挑战与下一步计划…'
									}
								/>
							</label>
							<div className="pptd-field-grid">
								<label className="pptd-field">
									<span>创作方式</span>
									<select
										value={preferences.workflow}
										disabled={busy || legacy}
										onChange={(event) =>
											update('workflow', event.target.value as Workflow)
										}
									>
										<option value="new">新建演示</option>
										<option value="edit" disabled={!project.pptd}>
											修改当前演示
										</option>
										<option value="recreate">参考重建</option>
									</select>
								</label>
								<label className="pptd-field">
									<span>页数</span>
									<div className="pptd-page-count">
										<select
											aria-label="页数模式"
											value={
												preferences.pageCount === 'auto' ? 'auto' : 'custom'
											}
											disabled={busy}
											onChange={(event) =>
												update(
													'pageCount',
													event.target.value === 'auto'
														? 'auto'
														: String(project.pptd?.pageCount || 10),
												)
											}
										>
											<option value="auto">自动规划</option>
											<option value="custom">指定页数</option>
										</select>
										{preferences.pageCount !== 'auto' && (
											<input
												aria-label="指定页数"
												type="number"
												min={1}
												max={60}
												value={preferences.pageCount}
												onChange={(event) =>
													update('pageCount', event.target.value)
												}
												disabled={busy}
											/>
										)}
									</div>
								</label>
								<label className="pptd-field">
									<span>使用场景</span>
									<select
										value={preferences.scenario}
										disabled={busy}
										onChange={(event) =>
											update('scenario', event.target.value as Scenario)
										}
									>
										{scenarios.map((item) => (
											<option key={item.value} value={item.value}>
												{item.label}
											</option>
										))}
									</select>
								</label>
								<label className="pptd-field">
									<span>使用方式</span>
									<select
										value={preferences.usage}
										disabled={busy}
										onChange={(event) =>
											update(
												'usage',
												event.target.value as 'reading' | 'presentation',
											)
										}
									>
										<option value="presentation">现场演讲</option>
										<option value="reading">独立阅读</option>
									</select>
								</label>
							</div>
							<label className="pptd-field">
								<span>受众</span>
								<input
									value={preferences.audience}
									disabled={busy}
									onChange={(event) => update('audience', event.target.value)}
									placeholder="例如：管理团队、客户、学生"
								/>
							</label>
							<label className="pptd-field">
								<span>视觉设计</span>
								<select
									value={preferences.designMode}
									disabled={busy}
									onChange={(event) =>
										update('designMode', event.target.value as DesignMode)
									}
								>
									<option value="autonomous">自主设计 · 根据内容选择</option>
									<option value="design-system">指定设计规范</option>
									<option value="template">使用公司模板</option>
									<option value="style-reference">参考视觉风格</option>
								</select>
							</label>
							{['design-system', 'style-reference'].includes(
								preferences.designMode,
							) && (
								<label className="pptd-field">
									<span>
										{preferences.designMode === 'design-system'
											? '设计规范'
											: '风格说明'}
									</span>
									{preferences.designMode === 'design-system' &&
										studio.designSystems.length > 0 && (
											<select
												aria-label="预设设计规范"
												value=""
												disabled={busy}
												onChange={(event) =>
													void loadDesignSystem(event.target.value)
												}
											>
												<option value="">从预设设计规范中选择…</option>
												{studio.designSystems.map((item) => (
													<option key={item.id} value={item.id}>
														{item.category} · {item.name}
													</option>
												))}
											</select>
										)}
									<textarea
										rows={3}
										value={preferences.designSystem}
										disabled={busy}
										onChange={(event) =>
											update('designSystem', event.target.value)
										}
										placeholder="描述字体、配色、排版与希望参考的视觉风格"
									/>
								</label>
							)}
							{preferences.designMode === 'template' && (
								<div className="pptd-template">
									<label className="pptd-field">
										<span>公司模板</span>
										<select
											aria-label="公司模板"
											value={templateId}
											disabled={busy}
											onChange={(event) => setTemplateId(event.target.value)}
										>
											<option value="">选择或上传模板</option>
											{studio.templates.map((item) => (
												<option key={item.id} value={item.id}>
													{item.name}
												</option>
											))}
										</select>
									</label>
									<button
										type="button"
										className="pptd-secondary"
										disabled={busy}
										onClick={() => templateInput.current?.click()}
									>
										<Upload size={14} />
										上传 PPTX / POTX
									</button>
									<input
										ref={templateInput}
										type="file"
										accept=".pptx,.potx"
										hidden
										onChange={(event) => void uploadTemplate(event)}
									/>
									{template?.warnings.map((warning) => (
										<p className="pptd-hint" key={warning}>
											{warning}
										</p>
									))}
								</div>
							)}
							{(preferences.workflow === 'recreate' ||
								preferences.designMode === 'style-reference' ||
								images.length > 0) && (
								<div className="pptd-reference-images">
									<span>视觉参考</span>
									<p>添加截图或设计参考图，让模型读取画面内容与风格。</p>
									<button
										type="button"
										className="pptd-secondary"
										disabled={busy || readingImages}
										onClick={() => imageInput.current?.click()}
									>
										<Upload size={14} />
										{readingImages ? '正在读取图片…' : '添加参考图片'}
									</button>
									<input
										ref={imageInput}
										aria-label="参考图片文件"
										type="file"
										accept="image/png,image/jpeg,image/webp"
										multiple
										hidden
										onChange={(event) => void readImages(event)}
									/>
									<div>
										{images.map((image, index) => (
											<figure key={`${image.name}-${index}`}>
												<img src={image.imageData} alt={image.name} />
												<figcaption>{image.name}</figcaption>
												<button
													type="button"
													className="studio-icon"
													aria-label={`移除参考图 ${image.name}`}
													disabled={busy || readingImages}
													onClick={() =>
														setImages((previous) =>
															previous.filter(
																(_, item) => item !== index,
															),
														)
													}
												>
													<X size={12} />
												</button>
											</figure>
										))}
									</div>
								</div>
							)}
							{!!project.importWarnings?.length && (
								<div className="pptd-import-warnings" role="status">
									<strong>模板导入说明</strong>
									{project.importWarnings.map((warning) => (
										<p className="pptd-hint" key={warning}>
											{warning}
										</p>
									))}
								</div>
							)}
							<details className="pptd-materials">
								<summary>
									<FileText size={14} />
									参考材料与内容选项
								</summary>
								<label className="pptd-field">
									<span>参考资料</span>
									<textarea
										rows={5}
										value={preferences.references}
										disabled={busy}
										onChange={(event) =>
											update('references', event.target.value)
										}
										placeholder="粘贴已有内容、大纲、数据或参考说明"
									/>
								</label>
								<button
									type="button"
									className="pptd-secondary"
									disabled={busy}
									onClick={() => referenceInput.current?.click()}
								>
									<Upload size={14} />
									添加文本资料
								</button>
								<input
									ref={referenceInput}
									type="file"
									accept=".txt,.md,.csv,.json"
									hidden
									onChange={(event) => void readReference(event)}
								/>
								<label className="pptd-check">
									<input
										type="checkbox"
										checked={preferences.allowResearch}
										disabled={busy}
										onChange={(event) =>
											update('allowResearch', event.target.checked)
										}
									/>
									允许检索外部资料
								</label>
								<label className="pptd-check">
									<input
										type="checkbox"
										checked={preferences.speakerNotes}
										disabled={busy}
										onChange={(event) =>
											update('speakerNotes', event.target.checked)
										}
									/>
									生成演讲备注
								</label>
								<label className="pptd-check">
									<input
										type="checkbox"
										checked={preferences.animationRequested}
										disabled={busy}
										onChange={(event) =>
											update('animationRequested', event.target.checked)
										}
									/>
									添加演示动画
								</label>
							</details>
							{project.pptd && preferences.workflow === 'edit' && (
								<label className="pptd-field">
									<span>修改范围</span>
									<select
										value={scope}
										disabled={busy}
										onChange={(event) =>
											setScope(event.target.value as 'page' | 'deck')
										}
									>
										<option value="page">当前第 {pageIndex + 1} 页</option>
										<option value="deck">整个演示</option>
									</select>
								</label>
							)}
							<button
								className="pptd-primary pptd-generate"
								type="submit"
								disabled={
									!canGenerate ||
									!prompt.trim() ||
									(preferences.designMode === 'template' && !templateId)
								}
							>
								<Sparkles size={17} />
								{project.pptd && preferences.workflow === 'edit'
									? '应用修改'
									: '生成演示'}
								<ArrowUpRight size={16} />
							</button>
						</form>
						{job && (
							<section
								className="pptd-progress"
								aria-label="生成进度"
								aria-live="polite"
							>
								<div>
									<strong>
										{studio.canStop ? (
											<LoaderCircle className="pptd-spin" size={15} />
										) : (
											<Layers size={15} />
										)}
										{statusLabels[job.status]}
									</strong>
									{studio.canStop && (
										<button type="button" onClick={() => void studio.stop()}>
											<Square size={12} />
											停止
										</button>
									)}
								</div>
								<p>{job.message}</p>
								{completedPages !== undefined && (
									<small>已生成 {completedPages} 页</small>
								)}
								{checkpoint && <small>恢复点：{checkpoint}</small>}
								{terminal && job.recoverable && checkpoint && (
									<button
										className="pptd-secondary"
										disabled={busy}
										onClick={() => void studio.resume()}
									>
										<RotateCcw size={14} />
										从检查点继续
									</button>
								)}
								{terminal && job.recoverable !== false && (
									<button
										className="pptd-secondary"
										disabled={busy}
										onClick={() => void studio.retry()}
									>
										<RotateCcw size={14} />
										重新开始任务
									</button>
								)}
								{!!job.events?.length && (
									<details>
										<summary>任务记录</summary>
										<ol>
											{job.events
												.filter((event) => event.message)
												.slice(-12)
												.map((event, index) => (
													<li key={index}>{event.message}</li>
												))}
										</ol>
										{job.traceId && <small>追踪编号：{job.traceId}</small>}
									</details>
								)}
							</section>
						)}
						{!!project.messages.length && (
							<details className="pptd-conversation">
								<summary>创作记录 · {project.messages.length}</summary>
								{project.messages.map((message) => (
									<article key={message.id}>
										<strong>
											{message.role === 'user' ? '我的要求' : '创作助手'}
										</strong>
										<p>{message.text}</p>
									</article>
								))}
							</details>
						)}
					</div>
				</main>
				<section
					ref={workbenchRef}
					className={`pptd-workbench ${presentation.presenting ? 'is-presenting' : ''}`}
					aria-label="演示文稿工作台"
				>
					<div className="pptd-preview-toolbar">
						<div className="pptd-tabs">
							<button
								className="pptd-settings-toggle"
								aria-label="创作设置"
								aria-expanded={briefOpen}
								aria-controls="pptd-creation-settings"
								onClick={() => setBriefPreference(!briefOpen)}
								title={briefOpen ? '收起创作设置' : '展开创作设置'}
							>
								{briefOpen ? (
									<PanelLeftClose size={16} />
								) : (
									<PanelLeftOpen size={16} />
								)}
								<span>创作设置</span>
							</button>
							<button
								className={mode === 'preview' ? 'is-active' : ''}
								onClick={() => setMode('preview')}
							>
								预览
							</button>
							<button
								className={mode === 'edit' ? 'is-active' : ''}
								disabled={!project.pptd || busy}
								onClick={() => setMode('edit')}
							>
								<Pencil size={13} />
								编辑
							</button>
						</div>
						<div className="pptd-preview-actions">
							{project.pptd && (
								<>
									<span className="pptd-page-indicator">
										{pageIndex + 1} / {project.pptd.pageCount}
									</span>
									<button
										className="pptd-present-button"
										disabled={busy || !pages.length}
										onClick={presentation.start}
										aria-label="全屏播放"
										title="全屏播放 · 方向键或滚轮翻页，Esc 退出"
									>
										<Maximize size={15} />
										<span>全屏播放</span>
									</button>
									<button
										className="studio-icon"
										title="版本记录"
										aria-label="版本记录"
										onClick={() => setShowVersions(!showVersions)}
									>
										<History size={16} />
									</button>
									<a
										className="studio-icon"
										title="在新窗口打开演示"
										aria-label="在新窗口打开演示"
										href={project.pptd.viewUrl}
										target="_blank"
										rel="noreferrer"
									>
										<ArrowUpRight size={17} />
									</a>
								</>
							)}
						</div>
					</div>
					{job && (
						<PptdJobStatus
							key={job.id}
							job={job}
							busy={busy}
							onStop={() => void studio.stop()}
							onResume={() => void studio.resume()}
							onRetry={() => void studio.retry()}
						/>
					)}
					{project.pptd ? (
						<>
							<div className="pptd-preview-stage">
								<nav
									ref={pageListRef}
									className="pptd-page-list"
									aria-label="演示页面"
									title="滚轮切换页面"
								>
									{pages.map((page, index) => (
										<button
											key={page.path}
											aria-current={
												currentPage?.path === page.path ? 'page' : undefined
											}
											className={
												currentPage?.path === page.path ? 'is-active' : ''
											}
											onClick={() => selectPage(page.path)}
											aria-label={`${String(index + 1).padStart(2, '0')} ${pageTitle(page, index)}`}
											title={pageTitle(page, index)}
										>
											<PptdThumbnail
												key={`${project.pptd?.revision}:${project.artifact?.id ?? ''}`}
												sources={pagePreviewSources(
													project,
													page.path,
													index,
													window.location.origin,
												)}
												size={document?.document.size}
											/>
											<span className="pptd-page-caption">
												<span>{String(index + 1).padStart(2, '0')}</span>
												<strong>{pageTitle(page, index)}</strong>
											</span>
										</button>
									))}
								</nav>
								<div className="pptd-frame-wrap">
									<iframe
										ref={iframeRef}
										src={iframeUrl}
										title="演示文稿预览与编辑器"
										onLoad={syncIframe}
										allow="fullscreen"
									/>
									{busy && (
										<div className="pptd-frame-busy">
											<LoaderCircle size={18} className="pptd-spin" />
											正在更新演示，完成后自动刷新
										</div>
									)}
								</div>
							</div>
							{presentation.presenting && (
								<div className="pptd-presentation-controls" aria-label="播放控制">
									<button
										aria-label="上一页"
										disabled={pageIndex === 0}
										onClick={() => presentation.navigate(-1)}
									>
										<ChevronLeft size={18} />
									</button>
									<span aria-live="polite">
										{pageIndex + 1} / {pages.length}
									</span>
									<button
										aria-label="下一页"
										onClick={() => presentation.navigate(1)}
									>
										<ChevronRight size={18} />
									</button>
									<span className="pptd-play-help">
										{presentation.windowOnly ? '窗口播放 · ' : ''}方向键 /
										滚轮翻页
									</span>
									<button aria-label="退出全屏" onClick={presentation.stop}>
										<Minimize size={16} />
										退出 <kbd>Esc</kbd>
									</button>
								</div>
							)}
							<footer className="pptd-export">
								<div>
									<strong>
										{currentPage
											? pageTitle(currentPage, pageIndex)
											: project.title}
									</strong>
								</div>
								<div className="pptd-export-buttons">
									{!!project.artifact?.warnings.length && (
										<details className="pptd-export-warnings">
											<summary>
												导出提示 · {project.artifact.warnings.length}
											</summary>
											<div role="status">
												{project.artifact.warnings.map((warning) => (
													<p key={warning}>{warning}</p>
												))}
											</div>
										</details>
									)}
									<a
										href={project.pptd.sourceUrl}
										className="pptd-secondary"
										download
									>
										<FileArchive size={15} />
										源文件 ZIP
									</a>
									{project.artifact ? (
										<>
											<a
												className="pptd-secondary"
												href={project.artifact.pptxUrl}
												download
											>
												<Download size={15} />
												PPTX
											</a>
											{project.artifact.pdfUrl && (
												<a
													className="pptd-secondary"
													href={project.artifact.pdfUrl}
													download
												>
													<Download size={15} />
													PDF
												</a>
											)}
										</>
									) : (
										<button
											className="pptd-primary"
											disabled={busy || !connected}
											onClick={() => void studio.start('render')}
										>
											<Download size={15} />
											导出 PPTX / PDF
										</button>
									)}
								</div>
							</footer>
						</>
					) : project.artifact?.previewUrls?.length ? (
						<ArtifactPreview artifact={project.artifact} title={project.title} />
					) : legacy ? (
						<div className="pptd-legacy">
							<span className="pptd-eyebrow">旧版工作记录</span>
							<h2>{project.title}</h2>
							<p>
								此记录保存了旧版页面草稿。迁移后即可使用新的预览、元素编辑和导出。
							</p>
							<button
								className="pptd-primary"
								disabled={busy || !connected}
								onClick={() => void studio.migrate()}
							>
								<Layers size={16} />
								迁移为可编辑演示
							</button>
							<label className="pptd-field">
								<span>旧版页面</span>
								<select
									value={legacyIndex}
									onChange={(event) => setLegacyIndex(Number(event.target.value))}
								>
									{project.slides.map((slide, index) => (
										<option key={slide.id} value={index}>
											{index + 1}. {slide.title}
										</option>
									))}
								</select>
							</label>
							<div className="pptd-legacy-preview">
								{project.artifact?.previewUrls[legacyIndex] ? (
									<img
										src={project.artifact.previewUrls[legacyIndex]}
										alt={`旧版第 ${legacyIndex + 1} 页`}
									/>
								) : (
									project.slides[legacyIndex] && (
										<SlideDraft
											slide={project.slides[legacyIndex]}
											index={legacyIndex}
											total={project.slides.length}
											theme={project.theme}
										/>
									)
								)}
							</div>
							<small>旧版预览 · 迁移前的内容</small>
						</div>
					) : (
						<div className="pptd-empty">
							<div className="pptd-empty-pages">
								<div />
								<div />
								<div>
									<Layers size={46} strokeWidth={1} />
									<span>YOUR NEXT IDEA</span>
								</div>
							</div>
							<h2>
								{studio.loading
									? '正在打开工作台…'
									: studio.canStop
										? '正在构建你的演示'
										: '每个好想法，都值得被看见'}
							</h2>
							<p>
								{studio.canStop
									? '页面生成后会在这里展示，保留文字、图形和图表的编辑能力。'
									: '从左侧输入主题开始，或从工作记录中继续创作。'}
							</p>
							{!studio.canStop && !studio.loading && (
								<button
									className="pptd-secondary"
									onClick={() => inputRef.current?.focus()}
								>
									写下第一个想法
									<ArrowUpRight size={14} />
								</button>
							)}
						</div>
					)}
					{showVersions && (
						<aside className="pptd-versions" aria-label="版本记录">
							<div>
								<strong>版本记录</strong>
								<button
									className="studio-icon"
									aria-label="关闭版本记录"
									onClick={() => setShowVersions(false)}
								>
									<X size={16} />
								</button>
							</div>
							<p>恢复会创建新版本，保留现有历史。</p>
							{[...versions].reverse().map((version) => (
								<article key={version.id}>
									<div>
										<strong>
											{version.label || `版本 ${version.revision}`}
										</strong>
										<small>
											版本 {version.revision}
											{version.createdAt
												? ` · ${new Date(version.createdAt).toLocaleString('zh-CN')}`
												: ''}
										</small>
									</div>
									<button
										className="pptd-secondary"
										disabled={
											busy || version.revision === project.pptd?.revision
										}
										onClick={() => void studio.restore(version.id)}
									>
										{version.revision === project.pptd?.revision
											? '当前'
											: '恢复'}
									</button>
								</article>
							))}
							{!versions.length && <p>版本记录正在加载。</p>}
						</aside>
					)}
				</section>
			</div>
			{deleteId && (
				<div className="pptd-modal-backdrop">
					<div
						className="pptd-modal"
						role="dialog"
						aria-modal="true"
						aria-label="删除工作记录"
					>
						<h2>删除这份工作记录？</h2>
						<p>删除后可以在工作记录中撤销。</p>
						<div>
							<button className="pptd-secondary" onClick={() => setDeleteId(null)}>
								取消
							</button>
							<button
								className="pptd-primary"
								onClick={() => {
									const record = studio.projects.find(
										(item) => item.id === deleteId,
									);
									if (record) void studio.deleteProject(record);
									setDeleteId(null);
								}}
							>
								删除记录
							</button>
						</div>
					</div>
				</div>
			)}
		</div>
	);
}
