import {
	ArrowDown,
	ArrowDownToLine,
	ArrowLeft,
	ArrowRight,
	ArrowUp,
	ArrowUpRight,
	Check,
	ChevronDown,
	ChevronLeft,
	ChevronRight,
	CircleHelp,
	Copy,
	Download,
	Expand,
	FileText,
	History,
	Layers,
	LayoutTemplate,
	LoaderCircle,
	Maximize2,
	MessageSquare,
	Minimize2,
	MoreHorizontal,
	Paperclip,
	Pause,
	Pencil,
	Play,
	Plus,
	RotateCcw,
	Sparkles,
	Square,
	Trash2,
	Undo2,
	X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import type { ProjectSummary } from './api';
import { downloadFile, uid, slideLayoutNames } from './model';
import type { Slide, Theme, SlideLayout } from './model';
import { PptdStudio } from './PptdStudio';
import { SlideDraft } from './SlideDraft';
import { ImageSettings, SlideImageEditor } from './SlideImages';
import { useStudio } from './useStudio';
import { WorkHistory } from './WorkHistory';
import './studio.css';

const steps = [
	['理解需求', '明确主题、受众与表达目标'],
	['构建大纲', '让每一页都有清晰的重点'],
	['设计页面', '编排内容，匹配视觉风格'],
	['完成演示', '检查版式，准备预览与播放'],
];
const themeNames = { forest: '苔绿', ink: '雾蓝', clay: '陶土' };

function IconButton({
	label,
	children,
	onClick,
	disabled,
	className = '',
}: {
	label: string;
	children: ReactNode;
	onClick?: () => void;
	disabled?: boolean;
	className?: string;
}) {
	return (
		<button
			type="button"
			className={`studio-icon ${className}`}
			title={label}
			aria-label={label}
			onClick={onClick}
			disabled={disabled}
		>
			{children}
		</button>
	);
}
function SlideCanvas({
	slide,
	index,
	total,
	theme,
	imageUrl,
	companyTemplate,
}: {
	slide: Slide;
	index: number;
	total: number;
	theme: Theme;
	imageUrl?: string;
	companyTemplate?: boolean;
}) {
	if (imageUrl)
		return (
			<img
				className="slide-rendered"
				src={imageUrl}
				alt={`第 ${index + 1} 页：${slide.title}`}
			/>
		);
	if (companyTemplate)
		return (
			<div className="template-preview-empty">
				<LayoutTemplate size={30} />
				<strong>{slide.title}</strong>
				<span>应用公司模板后，将在这里展示真实幻灯片</span>
			</div>
		);
	return <SlideDraft slide={slide} index={index} total={total} theme={theme} />;
}

export function StudioPage() {
	return <PptdStudio />;
}

export function LegacyStudioPage() {
	const studio = useStudio();
	return <StudioWorkspace key={studio.project.id} studio={studio} />;
}

function StudioWorkspace({ studio }: { studio: ReturnType<typeof useStudio> }) {
	const {
		project,
		projects,
		refreshProjects,
		deleteProject,
		lastDeleted,
		undoDelete,
		setProject,
		health,
		templates,
		job,
		error,
		setError,
		saveState,
		connected,
		uploading,
		busy,
		loading,
		canStop,
		start,
		stop,
		uploadTemplate,
		reconnect,
		openProject,
		newProject,
		localDraft,
		restoreLocalDraft,
		dismissLocalDraft,
	} = studio;
	const [deleteTarget, setDeleteTarget] = useState<ProjectSummary | null>(null);
	const [input, setInput] = useState('');
	const [selected, setSelected] = useState(0);
	const [expanded, setExpanded] = useState(false);
	const [tab, setTab] = useState<'preview' | 'edit'>('preview');
	const [modal, setModal] = useState<'history' | 'export' | 'help' | 'new' | 'delete' | null>(
		null,
	);
	const [playing, setPlaying] = useState(false);
	const [autoplay, setAutoplay] = useState(false);
	const stage = job?.stage ?? 0;
	const [notice, setNotice] = useState('');
	const [attachments, setAttachments] = useState<{ name: string; text: string }[]>([]);
	const pageCount = project.pageCount ?? 6;
	const audience = project.audience ?? '团队分享';
	const [undo, setUndo] = useState<Slide[][]>([]);
	const scrollRef = useRef<HTMLDivElement>(null);
	const followRef = useRef(false);
	const fileRef = useRef<HTMLInputElement>(null);
	const templateRef = useRef<HTMLInputElement>(null);
	const composerRef = useRef<HTMLTextAreaElement>(null);
	const [showScroll, setShowScroll] = useState(false);
	const selectedTemplate = templates.find((item) => item.id === project.templateId);
	const templateWarnings = [...new Set(selectedTemplate?.warnings ?? [])];
	const artifactWarnings = [...new Set(project.artifact?.warnings ?? [])].filter(
		(warning) => !templateWarnings.includes(warning),
	);
	const hasPreview = !!project.artifact?.previewUrls.length;
	const current = project.slides[Math.min(selected, project.slides.length - 1)];

	useEffect(() => {
		if (!notice) return;
		const timer = setTimeout(() => setNotice(''), 3500);
		return () => clearTimeout(timer);
	}, [notice]);
	useEffect(() => {
		if (followRef.current)
			scrollRef.current?.scrollTo({
				top: scrollRef.current.scrollHeight,
				behavior: 'smooth',
			});
	}, [project.messages, job?.message, project.phase]);
	useEffect(() => {
		if (!playing) return;
		const onKey = (event: KeyboardEvent) => {
			if (event.key === 'Escape') {
				setPlaying(false);
				setAutoplay(false);
			}
			if (['ArrowRight', 'ArrowDown', ' '].includes(event.key)) {
				event.preventDefault();
				setSelected((s) => Math.min(project.slides.length - 1, s + 1));
			}
			if (['ArrowLeft', 'ArrowUp'].includes(event.key)) {
				event.preventDefault();
				setSelected((s) => Math.max(0, s - 1));
			}
		};
		window.addEventListener('keydown', onKey);
		return () => window.removeEventListener('keydown', onKey);
	}, [playing, project.slides.length]);
	useEffect(() => {
		if (!playing || !autoplay) return;
		const timer = setInterval(() => setSelected((s) => (s + 1) % project.slides.length), 4500);
		return () => clearInterval(timer);
	}, [playing, autoplay, project.slides.length]);
	useEffect(() => {
		if (!modal && !playing) return;
		const previous = document.activeElement as HTMLElement | null;
		const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
		dialog?.querySelector<HTMLElement>('button')?.focus();
		const keydown = (e: KeyboardEvent) => {
			if (e.key === 'Escape') {
				setModal(null);
				setPlaying(false);
				setAutoplay(false);
			}
			if (e.key !== 'Tab' || !dialog) return;
			const items = dialog.querySelectorAll<HTMLElement>(
				'button:not(:disabled), input, textarea, select, [tabindex="0"]',
			);
			const first = items[0];
			const last = items[items.length - 1];
			if (e.shiftKey && document.activeElement === first) {
				e.preventDefault();
				last?.focus();
			} else if (!e.shiftKey && document.activeElement === last) {
				e.preventDefault();
				first?.focus();
			}
		};
		window.addEventListener('keydown', keydown);
		return () => {
			window.removeEventListener('keydown', keydown);
			previous?.focus();
		};
	}, [modal, playing]);

	async function submit(text = input) {
		if ((!text.trim() && !attachments.length) || busy) return;
		const prompt = text.trim() || '根据附件整理一份演示大纲';
		const references = [project.references, ...attachments.map((a) => a.text)]
			.filter(Boolean)
			.join('\n\n');
		if (references.length > 100000) {
			setError('参考资料累计超过 10 万字，请精简附件后重试。');
			return;
		}
		const next = {
			...project,
			references,
			pageCount,
			audience,
			title: project.title === '未命名演示' ? prompt.slice(0, 60) : project.title,
			messages: [
				...project.messages,
				{
					id: uid(),
					role: 'user' as const,
					text:
						prompt +
						(attachments.length
							? '\n\n📎 ' + attachments.map((a) => a.name).join('、')
							: ''),
				},
			],
		};
		followRef.current = true;
		const accepted = await start(
			(project.contentGenerated ??
				!!(project.slides.length || project.artifact || project.versions.length))
				? 'revise'
				: 'outline',
			{
				prompt,
				pageCount,
				audience,
				selectedSlideId: project.phase === 'ready' ? current?.id : undefined,
				references,
			},
			next,
		);
		if (accepted) {
			setInput('');
			setAttachments([]);
			setSelected(0);
			setUndo([]);
		}
	}
	function generate() {
		if (busy || !project.slides.length) return;
		setTab('preview');
		void start('render');
	}
	function changeSlides(slides: Slide[], keepUndo = true) {
		if (keepUndo) setUndo((u) => [...u.slice(-19), structuredClone(project.slides)]);
		setProject((p) => ({ ...p, slides }));
	}
	function editSlide(patch: Partial<Slide>) {
		if (!current) return;
		changeSlides(project.slides.map((s) => (s.id === current.id ? { ...s, ...patch } : s)));
	}
	function saveVersion() {
		if (!project.slides.length) return;
		setProject((p) => ({
			...p,
			versions: [
				{
					id: uid(),
					label: `手动保存 · ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`,
					title: p.title,
					slides: structuredClone(p.slides),
					theme: p.theme,
					templateId: p.templateId,
					imageSource: p.imageSource,
					artifact: p.artifact,
				},
				...p.versions,
			].slice(0, 12),
		}));
		setNotice('已保存一个可恢复的版本');
	}
	function moveSlide(direction: number) {
		const next = selected + direction;
		if (next < 0 || next >= project.slides.length) return;
		const slides = [...project.slides];
		[slides[selected], slides[next]] = [slides[next], slides[selected]];
		changeSlides(slides);
		setSelected(next);
	}
	const selectSlide = (index: number, edit = false) => {
		setSelected(index);
		if (edit) setTab('edit');
	};
	async function copy(text: string) {
		try {
			if (navigator.clipboard && window.isSecureContext)
				await navigator.clipboard.writeText(text);
			else {
				const el = document.createElement('textarea');
				el.value = text;
				document.body.appendChild(el);
				el.select();
				const copied = document.execCommand('copy');
				el.remove();
				if (!copied) throw new Error('copy');
			}
			setNotice('已复制到剪贴板');
		} catch {
			setNotice('浏览器限制了复制，请选中文字后手动复制');
		}
	}

	return (
		<div className="studio-app">
			<header className="studio-header">
				<a className="studio-brand" href="/" aria-label="叙页首页">
					<span className="brand-symbol">
						<Layers size={21} />
					</span>
					<strong>
						叙页<span>STUDIO</span>
					</strong>
				</a>
				<div className="header-divider" />
				<div className="project-breadcrumb">
					<span>我的工作空间</span>
					<ChevronRight size={14} />
					<input
						aria-label="演示文稿名称"
						disabled={busy}
						value={project.title}
						maxLength={180}
						onChange={(e) => setProject((p) => ({ ...p, title: e.target.value }))}
					/>
				</div>
				<div className="header-actions">
					<span className="saved-label">
						<span />
						{saveState}
					</span>
					<span className="demo-badge">
						{health?.llmConfigured ? 'AI 已连接' : '等待模型配置'}
					</span>
					<IconButton label="使用帮助" onClick={() => setModal('help')}>
						<CircleHelp size={18} />
					</IconButton>
					<button
						className="avatar"
						onClick={() => setModal('help')}
						aria-label="本地工作空间"
					>
						我
					</button>
				</div>
			</header>
			<div className="studio-body">
				<WorkHistory
					projects={projects}
					current={project}
					busy={busy}
					connected={connected}
					onOpen={(id) => void openProject(id)}
					onNew={() => void newProject()}
					onDelete={(item) => {
						setDeleteTarget(item);
						setModal('delete');
					}}
					onRefresh={() => void refreshProjects()}
					lastDeleted={lastDeleted}
					onUndo={() => void undoDelete()}
				/>
				<div className={`studio-workspace ${expanded ? 'is-expanded' : ''}`}>
					<main className="conversation">
						<div className="conversation-toolbar">
							<div>
								<span className="workspace-dot" />
								<strong className="project-picker">演示创作</strong>
								<span className="toolbar-tag">把想法变成有力的表达</span>
							</div>
							<div>
								<IconButton label="版本记录" onClick={() => setModal('history')}>
									<History size={17} />
								</IconButton>
								<button
									className="new-chat"
									onClick={() => setModal('new')}
									disabled={busy}
								>
									<Plus size={15} />
									新建演示
								</button>
							</div>
						</div>
						<div
							className="conversation-scroll"
							ref={scrollRef}
							onScroll={() => {
								const el = scrollRef.current;
								if (el) {
									followRef.current =
										el.scrollHeight - el.scrollTop - el.clientHeight < 100;
									setShowScroll(!followRef.current);
								}
							}}
						>
							<div className="chat-column">
								{localDraft && (
									<div className="studio-info">
										已加载服务器版本，本机不同的草稿已保留。
										<button disabled={busy} onClick={restoreLocalDraft}>
											恢复本机草稿
										</button>
										<button onClick={dismissLocalDraft}>使用服务器版本</button>
									</div>
								)}
								{error && (
									<div role="alert" className="studio-error">
										<strong>需要处理</strong>
										<p>{error}</p>
										<button onClick={() => void reconnect()} disabled={busy}>
											重新连接
										</button>
										<button onClick={() => setError('')}>关闭</button>
									</div>
								)}
								{connected && health && !health.llmConfigured && (
									<div className="studio-info">
										尚未配置语言模型。请在服务端配置模型后点击重新连接；已编辑的大纲仍可直接生成
										PPT。
										<button onClick={() => void reconnect()}>重新连接</button>
									</div>
								)}
								{health && !health.rendererAvailable && (
									<div className="studio-info">
										预览与 PDF 渲染服务暂不可用，生成后可下载 PPTX。
									</div>
								)}
								<div className="chat-intro">
									<span className="intro-kicker">
										<span /> YOUR IDEAS, BEAUTIFULLY TOLD
									</span>
									<h1>
										好想法，<span>值得好好呈现。</span>
									</h1>
									<p>聊聊你的想法，剩下的交给我们一起完成。</p>
								</div>
								<div className="date-divider">
									<span />
									今天 · 创作从这里开始
									<span />
								</div>
								{project.messages.map((message, index) => (
									<article
										className={`chat-message role-${message.role}`}
										key={message.id}
									>
										{message.role === 'assistant' && (
											<div className="assistant-avatar">
												<Sparkles size={17} />
											</div>
										)}
										<div className="message-main">
											{message.role === 'assistant' && (
												<div className="message-author">
													叙页 <span>创作伙伴</span>
												</div>
											)}
											<div className="message-text">{message.text}</div>
											{message.role === 'assistant' && (
												<div className="message-actions">
													<IconButton
														label="复制回复"
														onClick={() => void copy(message.text)}
													>
														<Copy size={13} />
													</IconButton>
													{index === project.messages.length - 1 && (
														<IconButton
															label="重新回复"
															disabled={busy}
															onClick={() => {
																const last = [...project.messages]
																	.reverse()
																	.find((m) => m.role === 'user');
																if (last) void submit(last.text);
															}}
														>
															<RotateCcw size={13} />
														</IconButton>
													)}
												</div>
											)}
										</div>
										{message.role === 'user' && (
											<span className="user-avatar">我</span>
										)}
									</article>
								))}
								{busy && (
									<div className="agent-working" role="status">
										<LoaderCircle className="spin" size={16} />
										<span>
											{uploading
												? '正在解析公司模板…'
												: loading
													? '正在连接服务…'
													: job?.message || '正在处理…'}
										</span>
									</div>
								)}
								{project.phase !== 'ready' && project.slides.length > 0 && (
									<div className="outline-card">
										<div className="outline-heading">
											<span className="outline-icon">
												<LayoutTemplate size={18} />
											</span>
											<div>
												<strong>演示大纲</strong>
												<p>
													{project.slides.length} 个章节 · 预计{' '}
													{project.slides.length * 2} 分钟
												</p>
											</div>
											<span className="outline-status">
												{project.phase === 'generating'
													? '正在生成'
													: '待你确认'}
											</span>
										</div>
										<div className="outline-list">
											{project.slides.map((slide, i) => (
												<button
													key={slide.id}
													onClick={() => selectSlide(i, true)}
													disabled={busy}
												>
													<span>{String(i + 1).padStart(2, '0')}</span>
													<strong>{slide.title}</strong>
													<small>
														{i === 0
															? '封面'
															: i === project.slides.length - 1
																? '结束页'
																: '内容页'}
													</small>
													<Pencil size={13} />
												</button>
											))}
										</div>
										<div className="outline-footer">
											<span>
												<Check size={13} />
												先确认结构，再打磨细节
											</span>
											<button
												className="primary-button"
												onClick={generate}
												disabled={busy}
											>
												{project.phase === 'generating' ? (
													<LoaderCircle size={14} className="spin" />
												) : (
													<Sparkles size={14} />
												)}
												{project.phase === 'generating'
													? '正在生成演示…'
													: '确认大纲并生成'}
												<ArrowRight size={14} />
											</button>
										</div>
									</div>
								)}
								{hasPreview && (
									<button
										className="completion-card"
										onClick={() => {
											setTab('preview');
											setExpanded(true);
										}}
									>
										<span className="completion-icon">
											<Check size={22} />
										</span>
										<div>
											<strong>你的演示，准备好了</strong>
											<p>
												{project.slides.length} 页幻灯片 ·{' '}
												{selectedTemplate?.name ??
													themeNames[project.theme]}{' '}
												· 点击查看
											</p>
										</div>
										<ArrowUpRight size={20} />
									</button>
								)}
								<div className="chat-end-marker">
									<span />
									一段对话，一个好作品
									<span />
								</div>
							</div>
						</div>
						<div className="composer-area">
							{showScroll && (
								<button
									className="scroll-bottom"
									onClick={() => {
										followRef.current = true;
										scrollRef.current?.scrollTo({
											top: scrollRef.current.scrollHeight,
											behavior: 'smooth',
										});
									}}
								>
									<ArrowDown size={15} />
									回到最新
								</button>
							)}
							<div className="suggestion-row">
								{(project.phase === 'ready'
									? ['让标题更简洁', '补充一个实际案例', '优化当前页的表达']
									: current
										? ['改成面向客户的提案', '我想换一个主题', '调整大纲结构']
										: [
												'帮我梳理演示思路',
												'制作一份项目汇报',
												'根据参考资料整理大纲',
											]
								).map((text) => (
									<button
										key={text}
										disabled={busy}
										onClick={() => {
											setInput(text);
											composerRef.current?.focus();
										}}
									>
										<Plus size={12} />
										{text}
									</button>
								))}
							</div>
							<div className="composer-box">
								{project.phase === 'ready' && (
									<div className="context-chip">
										<Layers size={12} />
										正在讨论第 {selected + 1} 页 · {current?.title}
									</div>
								)}
								{attachments.length > 0 && (
									<div className="attachments">
										{attachments.map((a, i) => (
											<span key={`${a.name}-${i}`}>
												<FileText size={13} />
												{a.name}
												<IconButton
													label={`移除 ${a.name}`}
													onClick={() =>
														setAttachments((list) =>
															list.filter((_, j) => j !== i),
														)
													}
												>
													<X size={12} />
												</IconButton>
											</span>
										))}
									</div>
								)}
								<textarea
									ref={composerRef}
									aria-label="输入你的演示需求"
									rows={2}
									placeholder="说说你的主题、听众，或者想要改变的地方…"
									value={input}
									onChange={(e) => setInput(e.target.value)}
									onKeyDown={(e) => {
										if (
											e.key === 'Enter' &&
											!e.shiftKey &&
											!e.nativeEvent.isComposing
										) {
											e.preventDefault();
											submit();
										}
									}}
								/>
								<div className="composer-controls">
									<div>
										<IconButton
											label="添加公司模板或参考资料（PPTX / POTX / TXT / Markdown）"
											disabled={busy}
											onClick={() => fileRef.current?.click()}
										>
											<Paperclip size={18} />
										</IconButton>
										<span className="control-divider" />
										<span className="assistant-select">
											<Sparkles size={14} />
											演示创作助手
										</span>
										<ChevronDown size={12} />
									</div>
									<div>
										<span className="enter-hint">Shift + Enter 换行</span>
										<button
											className={`send-button ${canStop ? 'is-busy' : ''}`}
											aria-label={canStop ? '停止生成' : '发送消息'}
											disabled={
												!canStop &&
												(busy || (!input.trim() && !attachments.length))
											}
											onClick={() => (canStop ? stop() : submit())}
										>
											{canStop ? (
												<Square size={16} fill="currentColor" />
											) : (
												<ArrowUp size={20} />
											)}
										</button>
									</div>
								</div>
							</div>
							<p className="composer-footnote">
								<span />
								{project.templateId
									? `公司模板 · ${selectedTemplate?.name ?? '已选择'}`
									: '从零创作 · 可上传公司 PPTX / POTX 模板'}
							</p>
						</div>
					</main>
					<aside className="preview-panel" aria-label="PPT 效果区域">
						<div className="preview-heading">
							<div>
								<span className="preview-icon">
									<Layers size={16} />
								</span>
								<strong>演示工作台</strong>
							</div>
							<IconButton
								label={expanded ? '收起效果区域' : '展开效果区域'}
								onClick={() => setExpanded(!expanded)}
							>
								{expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
							</IconButton>
						</div>
						<div className="preview-tabs">
							<button
								className={tab === 'preview' ? 'active' : ''}
								onClick={() => setTab('preview')}
							>
								<LayoutTemplate size={14} />
								预览
							</button>
							<button
								className={tab === 'edit' ? 'active' : ''}
								onClick={() => setTab('edit')}
								disabled={!current}
							>
								<Pencil size={14} />
								编辑
							</button>
							<span />
							<IconButton
								disabled={!current}
								label="导出与备份"
								onClick={() => setModal('export')}
							>
								<Download size={15} />
							</IconButton>
						</div>
						<div className="preview-scroll">
							<div className="company-template-card">
								<div className="section-label">
									<strong>
										<LayoutTemplate size={13} />
										公司模板
									</strong>
									<button
										disabled={busy}
										onClick={() => templateRef.current?.click()}
									>
										{uploading
											? '上传中…'
											: project.templateId
												? '替换模板'
												: '上传模板'}
									</button>
								</div>
								{templates.length > 0 && (
									<select
										aria-label="选择公司模板"
										disabled={busy}
										value={project.templateId ?? ''}
										onChange={(event) =>
											setProject((p) => ({
												...p,
												templateId: event.target.value || undefined,
											}))
										}
									>
										<option value="">从零生成 · 使用内置风格</option>
										{templates.map((item) => (
											<option key={item.id} value={item.id}>
												{item.name}
											</option>
										))}
									</select>
								)}
								{project.templateId ? (
									<>
										<div className="template-meta">
											<span>
												{selectedTemplate?.slideCount ?? '—'} 页样式参考
											</span>
											<button
												className="template-remove"
												disabled={busy}
												onClick={() =>
													setProject((p) => ({
														...p,
														templateId: undefined,
													}))
												}
											>
												移除模板
											</button>
										</div>
										{templateWarnings.length > 0 && (
											<details className="template-adaptation">
												<summary>
													模板适配说明（{templateWarnings.length} 项）
												</summary>
												<ul>
													{templateWarnings.map((warning) => (
														<li key={warning}>{warning}</li>
													))}
												</ul>
											</details>
										)}
									</>
								) : (
									<p>
										上传 PPTX / POTX，沿用公司的母版、配色和布局。最大 30 MB。
									</p>
								)}
							</div>
							<ImageSettings
								value={project.imageSource ?? 'auto'}
								busy={busy}
								status={health?.images}
								onChange={(imageSource) =>
									setProject((p) => ({ ...p, imageSource }))
								}
							/>
							{!project.artifact && project.phase === 'ready' && (
								<div className="studio-info">
									修改尚未应用到 PPT。
									<button onClick={generate} disabled={busy}>
										更新预览与导出
									</button>
								</div>
							)}
							{current ? (
								<>
									<div className="section-label">
										<span>
											{hasPreview
												? '实际 PPT 预览'
												: project.templateId
													? '公司模板 · 待生成'
													: '内容草稿 · 待生成'}
										</span>
										<span className="aspect-label">
											{hasPreview ? '真实版式' : '草稿'}
										</span>
									</div>
									<button
										className="slide-preview-button"
										aria-label="放大预览当前幻灯片"
										onClick={() => {
											setPlaying(true);
											setAutoplay(false);
										}}
									>
										<SlideCanvas
											slide={
												project.imageSource === 'none'
													? { ...current, imageDisabled: true }
													: current
											}
											index={selected}
											total={project.slides.length}
											theme={project.theme}
											imageUrl={project.artifact?.previewUrls[selected]}
											companyTemplate={!!project.templateId}
										/>
										<span className="preview-hover">
											<Expand size={16} />
											放大预览
										</span>
									</button>
									<div className="slide-pagination">
										<span>
											{String(selected + 1).padStart(2, '0')}
											<small>
												{' '}
												/ {String(project.slides.length).padStart(2, '0')}
											</small>
										</span>
										<div>
											<IconButton
												label="上一页"
												disabled={selected === 0}
												onClick={() => setSelected(selected - 1)}
											>
												<ChevronLeft size={15} />
											</IconButton>
											<IconButton
												label="下一页"
												disabled={selected === project.slides.length - 1}
												onClick={() => setSelected(selected + 1)}
											>
												<ChevronRight size={15} />
											</IconButton>
										</div>
									</div>
								</>
							) : (
								<div className="empty-preview">
									<Layers size={34} strokeWidth={1.3} />
									<strong>你的演示将从这里开始</strong>
									<p>
										在左侧描述主题与听众，或先上传公司模板。
										<br />
										生成后，即可在这里预览、编辑和播放。
									</p>
								</div>
							)}
							{artifactWarnings.length > 0 && (
								<div className="artifact-checks" role="status">
									<strong>生成检查（{artifactWarnings.length} 项）</strong>
									<ul>
										{artifactWarnings.slice(0, 2).map((warning) => (
											<li key={warning}>{warning}</li>
										))}
									</ul>
									{artifactWarnings.length > 2 && (
										<details>
											<summary>
												查看其余 {artifactWarnings.length - 2} 项提醒
											</summary>
											<ul>
												{artifactWarnings.slice(2).map((warning) => (
													<li key={warning}>{warning}</li>
												))}
											</ul>
										</details>
									)}
								</div>
							)}
							{tab === 'preview' || !current ? (
								<>
									{project.phase !== 'ready' ? (
										<div className="generation-card">
											<div className="section-label">
												<strong>创作进度</strong>
												<span>
													{project.phase === 'generating'
														? job?.status === 'queued'
															? '排队中'
															: '处理中'
														: current
															? '等待确认'
															: '等待输入'}
												</span>
											</div>
											<div className="progress-track">
												<span
													style={{
														width:
															project.phase === 'generating'
																? `${Math.min(95, (stage + 1) * 24)}%`
																: '0%',
													}}
												/>
											</div>
											<div className="generation-steps">
												{steps.map(([title, detail], i) => {
													const active =
														project.phase === 'generating'
															? stage
															: current
																? 0
																: -1;
													return (
														<div
															key={title}
															className={`generation-step ${i < active ? 'done' : i === active ? 'current' : ''}`}
														>
															<span className="step-marker">
																{i < active ? (
																	<Check size={12} />
																) : i === active &&
																  project.phase === 'generating' ? (
																	<LoaderCircle
																		size={12}
																		className="spin"
																	/>
																) : (
																	<span />
																)}
															</span>
															<div>
																<strong>{title}</strong>
																<p>{detail}</p>
															</div>
															{i === active && (
																<span className="step-label">
																	{project.phase === 'generating'
																		? '进行中'
																		: '待确认'}
																</span>
															)}
														</div>
													);
												})}
											</div>
											<div className="generation-tip">
												<Sparkles size={13} />
												<span>
													{project.phase === 'generating'
														? job?.message || '正在启动任务…'
														: current
															? '确认左侧大纲，即可开始生成'
															: '输入需求后，创作进度会显示在这里'}
												</span>
											</div>
										</div>
									) : (
										<div className="ready-status">
											<span>
												<Check size={14} />
												{hasPreview
													? '全部页面已就绪'
													: '内容已修改，请更新预览'}
											</span>
											<button
												onClick={generate}
												disabled={busy}
												title="重新生成演示"
											>
												<RotateCcw size={13} />
												更新预览
											</button>
										</div>
									)}
									<div className="section-label theme-label">
										<strong>视觉风格</strong>
										<span>
											{project.templateId ? '已使用公司模板' : '随时切换'}
										</span>
									</div>
									<div className="theme-options">
										{(['forest', 'ink', 'clay'] as Theme[]).map((theme) => (
											<button
												key={theme}
												disabled={busy || !!project.templateId}
												aria-pressed={project.theme === theme}
												className={`theme-option ${project.theme === theme ? 'selected' : ''}`}
												onClick={() => setProject((p) => ({ ...p, theme }))}
											>
												<span className={`theme-swatch theme-${theme}`}>
													<span />
													Aa
													{project.theme === theme && <Check size={12} />}
												</span>
												<span>{themeNames[theme]}</span>
											</button>
										))}
									</div>
									{project.phase === 'ready' && (
										<>
											<div className="section-label">
												<strong>全部页面</strong>
												<span>{project.slides.length} 页</span>
											</div>
											<div className="slide-grid">
												{project.slides.map((s, i) => (
													<button
														key={s.id}
														className={selected === i ? 'selected' : ''}
														onClick={() => selectSlide(i)}
														aria-label={`预览第 ${i + 1} 页：${s.title}`}
													>
														<SlideCanvas
															slide={
																project.imageSource === 'none'
																	? { ...s, imageDisabled: true }
																	: s
															}
															index={i}
															total={project.slides.length}
															theme={project.theme}
															imageUrl={
																project.artifact?.previewUrls[i]
															}
															companyTemplate={!!project.templateId}
														/>
														<span>
															{String(i + 1).padStart(2, '0')}
															<small>{s.title}</small>
														</span>
													</button>
												))}
											</div>
										</>
									)}
									{project.phase === 'outline' && (
										<div className="preferences">
											<div className="section-label">
												<strong>创作偏好</strong>
												<span>用于下次对话</span>
											</div>
											<label>
												页数
												<select
													value={pageCount}
													onChange={(e) =>
														setProject((p) => ({
															...p,
															pageCount: Number(e.target.value),
														}))
													}
												>
													{[4, 6, 8, 10].map((n) => (
														<option key={n} value={n}>
															{n} 页
														</option>
													))}
												</select>
											</label>
											<label>
												使用场景
												<select
													value={audience}
													onChange={(e) =>
														setProject((p) => ({
															...p,
															audience: e.target.value,
														}))
													}
												>
													{[
														'团队分享',
														'客户提案',
														'项目汇报',
														'课堂教学',
													].map((a) => (
														<option key={a}>{a}</option>
													))}
												</select>
											</label>
										</div>
									)}
								</>
							) : (
								<div className="slide-editor">
									<SlideImageEditor
										key={current.id}
										slide={
											project.imageSource === 'none'
												? { ...current, imageDisabled: true }
												: current
										}
										source={project.imageSource ?? 'auto'}
										busy={busy}
										onChange={editSlide}
									/>
									<div className="section-label">
										<strong>编辑第 {selected + 1} 页</strong>
										<IconButton
											label="撤销修改"
											disabled={!undo.length || busy}
											onClick={() => {
												const previous = undo.at(-1);
												if (previous) {
													changeSlides(previous, false);
													setUndo((u) => u.slice(0, -1));
													setSelected(
														Math.min(selected, previous.length - 1),
													);
												}
											}}
										>
											<Undo2 size={15} />
										</IconButton>
									</div>
									<fieldset disabled={busy}>
										<label>
											页面版式
											<select
												aria-label="页面版式"
												value={current.layout ?? 'auto'}
												disabled={!!project.templateId}
												onChange={(event) =>
													editSlide({
														layout: event.target.value as SlideLayout,
													})
												}
											>
												{Object.entries(slideLayoutNames).map(
													([value, label]) => (
														<option key={value} value={value}>
															{label}
														</option>
													),
												)}
											</select>
											<span className="layout-editor-hint">
												{project.templateId
													? '公司模板使用原有布局。'
													: '更新预览后生效。双项对比需两条要点，步骤流程需 2–5 条；内容不适合时自动调整。'}
											</span>
										</label>
										<label>
											章节标签
											<input
												maxLength={60}
												value={current.section ?? ''}
												onChange={(event) =>
													editSlide({ section: event.target.value })
												}
											/>
										</label>
										<label>
											标题
											<input
												maxLength={60}
												value={current?.title}
												onChange={(e) =>
													editSlide({ title: e.target.value })
												}
											/>
										</label>
										<label>
											副标题
											<textarea
												rows={2}
												maxLength={300}
												value={current.subtitle}
												onChange={(e) =>
													editSlide({ subtitle: e.target.value })
												}
											/>
										</label>
										<label>
											正文要点 <small>每行一条，建议 3–5 条</small>
											<textarea
												rows={5}
												value={current.points.join('\n')}
												onChange={(e) =>
													editSlide({
														points: e.target.value.split('\n'),
													})
												}
											/>
										</label>
										<label>
											演讲备注
											<textarea
												rows={3}
												maxLength={5000}
												value={current.notes}
												onChange={(e) =>
													editSlide({ notes: e.target.value })
												}
												placeholder="写下这一页想补充讲述的内容…"
											/>
										</label>
										<label>
											内容来源与口径
											<textarea
												rows={2}
												maxLength={800}
												value={current.source ?? ''}
												placeholder="填写真实来源、时间、数据口径或原文链接（可留空）"
												onChange={(event) =>
													editSlide({ source: event.target.value })
												}
											/>
										</label>
										<div className="edit-actions">
											<IconButton
												label="向前移动此页"
												disabled={selected === 0}
												onClick={() => moveSlide(-1)}
											>
												<ArrowLeft size={15} />
											</IconButton>
											<IconButton
												label="向后移动此页"
												disabled={selected === project.slides.length - 1}
												onClick={() => moveSlide(1)}
											>
												<ArrowRight size={15} />
											</IconButton>
											<IconButton
												label="复制当前页"
												onClick={() => {
													const slides = [...project.slides];
													slides.splice(selected + 1, 0, {
														...structuredClone(current),
														id: uid(),
													});
													changeSlides(slides);
													setSelected(selected + 1);
												}}
											>
												<Copy size={15} />
											</IconButton>
											<IconButton
												label="删除当前页"
												disabled={project.slides.length === 1}
												onClick={() => {
													changeSlides(
														project.slides.filter(
															(s) => s.id !== current.id,
														),
													);
													setSelected(Math.max(0, selected - 1));
												}}
											>
												<Trash2 size={15} />
											</IconButton>
											<button
												onClick={() => {
													changeSlides([
														...project.slides,
														{
															id: uid(),
															title: '新的想法',
															subtitle: '在这里补充副标题',
															points: ['写下你的第一个要点'],
															notes: '',
														},
													]);
													setSelected(project.slides.length);
												}}
											>
												<Plus size={14} />
												加一页
											</button>
										</div>
									</fieldset>
									<button
										className="version-button"
										onClick={saveVersion}
										disabled={busy}
									>
										<History size={14} />
										保存当前版本
									</button>
									<button
										className="version-button"
										onClick={generate}
										disabled={busy}
									>
										<Sparkles size={14} />
										应用修改并更新预览
									</button>
									<p className="editor-hint">
										内容自动保存到服务器。编辑后点击「应用修改并更新预览」，重新生成实际
										PPT 和导出文件。
									</p>
								</div>
							)}
						</div>
						<div className="preview-footer">
							<button
								className="play-button"
								disabled={!hasPreview || busy}
								onClick={() => {
									setSelected(0);
									setPlaying(true);
								}}
							>
								<Play size={15} fill="currentColor" />
								播放演示
							</button>
							<IconButton
								disabled={!current}
								label="更多导出选项"
								onClick={() => setModal('export')}
							>
								<MoreHorizontal size={19} />
							</IconButton>
						</div>
					</aside>
				</div>
			</div>
			<input
				hidden
				type="file"
				ref={templateRef}
				accept=".pptx,.potx"
				onChange={(event) => {
					const file = event.target.files?.[0];
					event.target.value = '';
					if (file) void uploadTemplate(file);
				}}
			/>
			<input
				hidden
				type="file"
				ref={fileRef}
				accept=".pptx,.potx,.txt,.md"
				multiple
				onChange={async (event) => {
					const files = Array.from(event.target.files ?? []);
					event.target.value = '';
					const companyFiles = files.filter((file) => /\.(pptx|potx)$/i.test(file.name));
					if (companyFiles[0]) await uploadTemplate(companyFiles[0]);
					if (companyFiles.length > 1)
						setNotice('每份演示使用一个公司模板，已选择第一个文件');
					const textFiles = files.filter((file) => !/\.(pptx|potx)$/i.test(file.name));
					const accepted = textFiles.filter(
						(f) => /\.(txt|md)$/i.test(f.name) && f.size <= 1024 * 1024,
					);
					if (accepted.length !== textFiles.length)
						setNotice('仅支持 1 MB 以内的 TXT / Markdown 文件');
					const contents = await Promise.all(
						accepted
							.slice(0, 5)
							.map(async (file) => ({ name: file.name, text: await file.text() })),
					);
					setAttachments((list) => [...list, ...contents].slice(0, 5));
				}}
			/>
			{notice && (
				<div role="status" className="studio-toast">
					<Check size={16} />
					{notice}
				</div>
			)}
			{playing && current && (
				<div
					className="presentation-overlay"
					role="dialog"
					aria-modal="true"
					aria-label="幻灯片播放"
				>
					<div className="presentation-header">
						<span>
							叙页 STUDIO <small>{hasPreview ? '演示播放' : '草稿预览'}</small>
						</span>
						<IconButton
							label="退出播放"
							onClick={() => {
								setPlaying(false);
								setAutoplay(false);
							}}
						>
							<X size={22} />
						</IconButton>
					</div>
					<div className="presentation-stage">
						<SlideCanvas
							slide={
								project.imageSource === 'none'
									? { ...current, imageDisabled: true }
									: current
							}
							index={selected}
							total={project.slides.length}
							theme={project.theme}
							imageUrl={project.artifact?.previewUrls[selected]}
							companyTemplate={!!project.templateId}
						/>
					</div>
					<div className="presentation-controls">
						<span>← → 切换 · Esc 退出</span>
						<div>
							<IconButton
								label="播放上一页"
								disabled={selected === 0}
								onClick={() => setSelected(selected - 1)}
							>
								<ChevronLeft size={20} />
							</IconButton>
							<span>
								{selected + 1} / {project.slides.length}
							</span>
							<IconButton
								label="播放下一页"
								disabled={selected === project.slides.length - 1}
								onClick={() => setSelected(selected + 1)}
							>
								<ChevronRight size={20} />
							</IconButton>
							<IconButton
								label={autoplay ? '暂停自动播放' : '自动播放'}
								onClick={() => setAutoplay(!autoplay)}
							>
								{autoplay ? <Pause size={18} /> : <Play size={18} />}
							</IconButton>
						</div>
						<span>{autoplay ? '每 4.5 秒自动翻页' : '手动播放'}</span>
					</div>
				</div>
			)}
			{modal && (
				<div className="modal-backdrop" onClick={() => setModal(null)}>
					<section
						className="studio-modal"
						role="dialog"
						aria-modal="true"
						aria-labelledby="modal-title"
						onClick={(e) => e.stopPropagation()}
					>
						<div className="modal-heading">
							<h2 id="modal-title">
								{
									{
										history: '版本记录',
										export: '带走你的好想法',
										help: '欢迎来到叙页',
										new: '开启一段新的创作',
										delete: '删除工作记录',
									}[modal]
								}
							</h2>
							<IconButton label="关闭弹窗" onClick={() => setModal(null)}>
								<X size={20} />
							</IconButton>
						</div>
						{modal === 'delete' && deleteTarget && (
							<>
								<p className="modal-description">
									删除「{deleteTarget.title || '未命名演示'}
									」及其对话、页面和版本记录？删除后可在左侧撤销，公司模板库不受影响。
								</p>
								<div className="new-actions">
									<button
										className="secondary-button"
										disabled={busy}
										onClick={() => setModal(null)}
									>
										取消
									</button>
									<button
										className="danger-button"
										disabled={busy}
										onClick={() => {
											void deleteProject(deleteTarget).then((deleted) => {
												if (deleted) setModal(null);
											});
										}}
									>
										<Trash2 size={14} />
										确认删除
									</button>
								</div>
							</>
						)}
						{modal === 'history' && (
							<>
								<p className="modal-description">
									版本保存在服务器，可恢复内容、配色和模板。
								</p>
								<button
									className="primary-button"
									onClick={saveVersion}
									disabled={busy}
								>
									<Plus size={15} />
									保存当前版本
								</button>
								<div className="version-list">
									{project.versions.length ? (
										project.versions.map((v) => (
											<div key={v.id}>
												<History size={17} />
												<div>
													<strong>{v.label}</strong>
													<small>
														{v.slides.length} 页 · {themeNames[v.theme]}
													</small>
												</div>
												<button
													disabled={busy}
													onClick={() => {
														changeSlides(structuredClone(v.slides));
														setProject((p) => ({
															...p,
															theme: v.theme,
															phase:
																v.artifact && v.title
																	? 'ready'
																	: 'outline',
															title: v.title ?? p.title,
															templateId: v.templateId,
															imageSource: v.imageSource,
															artifact: v.title
																? v.artifact
																: undefined,
														}));
														setSelected(0);
														setModal(null);
														setNotice(
															'已恢复所选版本，可在编辑区域撤销内容更改',
														);
													}}
												>
													恢复
												</button>
											</div>
										))
									) : (
										<div className="empty-versions">
											还没有保存的版本。生成完成后会自动保存一份。
										</div>
									)}
								</div>
							</>
						)}
						{modal === 'export' && (
							<>
								<p className="modal-description">
									下载可编辑 PPTX、原版式 PDF 或内容备份。
								</p>
								{(['pptx', 'pdf'] as const).map((format) => {
									const url =
										format === 'pptx'
											? project.artifact?.pptxUrl
											: project.artifact?.pdfUrl;
									return (
										<button
											className="export-option"
											key={format}
											disabled={!url || busy}
											onClick={() => {
												if (url) {
													const link = document.createElement('a');
													link.href = url;
													link.download = '';
													link.click();
												}
											}}
										>
											<FileText size={23} />
											<div>
												<strong>{format.toUpperCase()}</strong>
												<span>
													{url
														? format === 'pptx'
															? '可在 PowerPoint 中继续编辑'
															: '与实际预览相同的页面版式'
														: '请先生成或更新演示'}
												</span>
											</div>
											<ArrowDownToLine size={18} />
										</button>
									);
								})}
								<button
									className="export-option"
									onClick={() => {
										downloadFile(
											'叙页-内容备份.json',
											JSON.stringify(project, null, 2),
											'application/json',
										);
										setNotice('已导出内容备份');
									}}
								>
									<Download size={23} />
									<div>
										<strong>内容备份 · JSON</strong>
										<span>保存全部页面、演讲备注与历史版本。</span>
									</div>
									<ArrowDownToLine size={18} />
								</button>
								<p className="export-note">
									编辑内容或切换模板后，需要更新演示才能下载最新文件。
								</p>
							</>
						)}
						{modal === 'help' && (
							<>
								<p className="modal-description">
									从一句想法，到一份有设计感的演示。
								</p>
								<ol className="help-list">
									<li>
										<MessageSquare size={19} />
										<div>
											<strong>聊聊你的需求</strong>
											<p>
												输入主题和听众，可上传公司 PPTX / POTX 模板及 TXT /
												Markdown 参考文本。
											</p>
										</div>
									</li>
									<li>
										<LayoutTemplate size={19} />
										<div>
											<strong>确认大纲，再开始设计</strong>
											<p>
												点击章节编辑内容，确认后生成可编辑
												PPT，并查看真实生成进度。
											</p>
										</div>
									</li>
									<li>
										<Pencil size={19} />
										<div>
											<strong>让每一页更像你的作品</strong>
											<p>
												切换配色、编辑内容、调整页序；展开工作台获得更大空间。
											</p>
										</div>
									</li>
									<li>
										<Play size={19} />
										<div>
											<strong>准备好，就开始讲述</strong>
											<p>支持大屏播放、方向键切换与自动播放。</p>
										</div>
									</li>
								</ol>
								<p className="export-note">
									项目保存在服务器。生成结果请核验事实与数据；公司模板会尽量保留母版、布局与品牌元素，复杂图表或动画可能存在差异。
								</p>
							</>
						)}
						{modal === 'new' && (
							<>
								<p className="modal-description">
									当前演示会自动保存到左侧「工作记录」。点击记录即可切换并继续创作。
								</p>
								<div className="new-actions">
									<button
										className="secondary-button"
										onClick={() =>
											downloadFile(
												'叙页-内容备份.json',
												JSON.stringify(project, null, 2),
												'application/json',
											)
										}
									>
										先导出备份
									</button>
									<button
										className="primary-button"
										onClick={() => {
											void newProject();
										}}
									>
										新建演示
										<ArrowRight size={14} />
									</button>
								</div>
							</>
						)}
					</section>
				</div>
			)}
		</div>
	);
}
