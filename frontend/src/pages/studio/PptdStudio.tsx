import {
	ArrowUpRight,
	ChevronLeft,
	ChevronRight,
	History,
	Layers,
	LoaderCircle,
	Maximize,
	Minimize,
	PanelLeftClose,
	PanelLeftOpen,
	Pencil,
	RotateCcw,
	Sparkles,
	Square,
} from 'lucide-react';
import { useTheme } from 'next-themes';
import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';

import { studioApi, StudioError } from './api';
import type { ReferenceBudget, ReferenceFile } from './api';
import { PageActions } from './PageActions';
import { PptdJobStatus } from './PptdJobStatus';
import {
	defaultPreferences,
	generationOptions,
	groupImportWarnings,
	isLegacyProject,
	pageTitle,
	pagePreviewSources,
} from './pptdState';
import type { GenerationPreferences } from './pptdState';
import { PptdThumbnail } from './PptdThumbnail';
import { readReferenceDocument } from './referenceDocument';
import { StudioCreationSettings } from './StudioCreationSettings';
import { StudioDialogs } from './StudioDialogs';
import { StudioExportBar } from './StudioExportBar';
import { formatCharacters } from './studioFormat';
import { StudioHeader } from './StudioHeader';
import { StudioMaterialsPanel } from './StudioMaterialsPanel';
import {
	StudioDraftPreview,
	StudioEmptyState,
	StudioLegacyPreview,
	StudioVersionHistory,
} from './StudioPreviewStates';
import { usePptdStudio } from './usePptdStudio';
import { useMediaQuery, usePageWheel, usePresentation } from './usePreviewNavigation';
import { WorkHistory } from './WorkHistory';
import './studio.css';
import './pptd-studio.css';
import './studio-theme.css';

const statusLabels = {
	queued: '等待开始',
	running: '正在创作',
	completed: '已完成',
	failed: '任务失败',
	cancelled: '已停止',
	interrupted: '任务中断',
};
type ReferenceAttachment = ReferenceFile & { text?: string };
function composeReferenceText(manual: string, attachments: { name: string; text?: string }[]) {
	let result = manual.trim();
	for (const attachment of attachments) {
		if (!attachment.text) continue;
		const section = `【${attachment.name}】\n${attachment.text}`.trim();
		if (!result.includes(section)) result = [result, section].filter(Boolean).join('\n\n');
	}
	return result;
}
function removeReferenceSection(manual: string, name: string, text: string) {
	const section = `【${name}】\n${text}`.trim();
	return manual
		.replace(section, '')
		.replace(/\n{3,}/g, '\n\n')
		.trim();
}

export function PptdStudio() {
	const studio = usePptdStudio();
	return <PptdWorkspace key={studio.project.id} studio={studio} />;
}
function PptdWorkspace({ studio }: { studio: ReturnType<typeof usePptdStudio> }) {
	const { resolvedTheme } = useTheme();
	const { project, document, health, job, busy, connected, setError } = studio;
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
	const [readingReference, setReadingReference] = useState(false);
	const [referenceBudget, setReferenceBudget] = useState<ReferenceBudget | null>(null);
	const [referenceBudgetLoading, setReferenceBudgetLoading] = useState(false);
	const [referenceAttachments, setReferenceAttachments] = useState<ReferenceAttachment[]>([]);
	const [referencePreview, setReferencePreview] = useState<{
		name: string;
		text: string;
		downloadUrl: string;
	} | null>(null);
	const [previewingReference, setPreviewingReference] = useState(false);
	const [selectedPage, setSelectedPage] = useState('');
	const [legacyIndex, setLegacyIndex] = useState(0);
	const [mode, setMode] = useState<'preview' | 'edit'>('preview');
	const [showVersions, setShowVersions] = useState(false);
	const [scope, setScope] = useState<'page' | 'deck'>('page');
	const [expertMode, setExpertMode] = useState(false);
	const [notice, setNotice] = useState('');
	const [deleteId, setDeleteId] = useState<string | null>(null);
	useEffect(() => {
		if (job?.id && window.matchMedia('(max-width: 760px)').matches) {
			window.document.querySelector('.pptd-live-status')?.scrollIntoView({ block: 'start' });
		}
	}, [job?.id]);
	useEffect(() => {
		let disposed = false;
		if (!connected || !health?.llmConfigured) {
			setReferenceBudget(null);
			setReferenceBudgetLoading(false);
			return;
		}
		setReferenceBudgetLoading(true);
		void studioApi
			.referenceBudget()
			.then((budget) => {
				if (!disposed) setReferenceBudget(budget);
			})
			.catch(() => {
				if (!disposed) setReferenceBudget(null);
			})
			.finally(() => {
				if (!disposed) setReferenceBudgetLoading(false);
			});
		return () => {
			disposed = true;
		};
	}, [connected, health?.llmConfigured, health?.model]);
	useEffect(() => {
		let disposed = false;
		void studioApi
			.referenceFiles(project.id)
			.then((files) => {
				if (!disposed) setReferenceAttachments(files);
			})
			.catch((cause: unknown) => {
				if (!disposed)
					setError(
						cause instanceof StudioError && cause.status === 404
							? '参考资料服务尚未加载，请重启智能体服务后刷新页面。'
							: cause instanceof Error
								? cause.message
								: '无法读取参考资料列表。',
					);
			});
		return () => {
			disposed = true;
		};
	}, [project.id, setError]);
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
	const pageAction = job?.status === 'completed' ? job.result?.pageAction : undefined;
	const liveDraft = job?.draftPreview?.pages.some((page) => page.previewUrl)
		? job.draftPreview
		: null;
	const pageActionJob = !!job?.kind?.startsWith('page-');
	const template = studio.templates.find((item) => item.id === templateId);
	const referenceText = composeReferenceText(preferences.references, referenceAttachments);
	const canGenerate =
		!busy &&
		!readingImages &&
		!readingReference &&
		connected &&
		!!health?.llmConfigured &&
		!legacy;
	const terminal = job && ['failed', 'cancelled', 'interrupted'].includes(job.status);
	const checkpoint =
		typeof job?.checkpoint === 'string' ? job.checkpoint : job?.checkpoint?.summary;
	const completedPages =
		job?.checkpoint &&
		typeof job.checkpoint === 'object' &&
		typeof job.checkpoint.completedPages === 'number'
			? job.checkpoint.completedPages
			: undefined;
	const importWarnings = groupImportWarnings(project.importWarnings);
	const importWarningCount = importWarnings.reduce((total, item) => total + item.count, 0);
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
	async function submit(event: FormEvent) {
		event.preventDefault();
		if (!prompt.trim() || !canGenerate) return;
		try {
			const references = composeReferenceText(preferences.references, referenceAttachments);
			if (references.length > 0) {
				const budget = referenceBudget ?? (await studioApi.referenceBudget());
				if (!referenceBudget) setReferenceBudget(budget);
				if (references.length > budget.maxCharacters)
					throw new Error(
						`当前模型参考资料上限为 ${formatCharacters(budget.maxCharacters)}；` +
							`当前为 ${formatCharacters(references.length)}，请精简后重试。`,
					);
			}
			const kind = expertMode
				? 'kimi-slides'
				: project.pptd && preferences.workflow === 'edit'
					? 'revise'
					: 'outline';
			const started = await studio.start(kind, {
				...generationOptions({ ...preferences, references }),
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
	async function startPageAction(kind: 'page-repair' | 'page-review', description: string) {
		if (!currentPage || busy) return false;
		return studio.start(kind, {
			prompt: description,
			selectedSlideId: currentPage.path,
			scenario: preferences.scenario,
			usage: preferences.usage,
			templateId: project.templateId,
		});
	}
	async function startPageDesign(description: string) {
		if (!currentPage || busy) return false;
		return studio.start('page-design', {
			prompt: description,
			selectedSlideId: currentPage.path,
			scenario: preferences.scenario,
			usage: preferences.usage,
			templateId: project.templateId,
		});
	}
	async function applyDesignCandidate(candidateId: string) {
		if (!project.pptd) return;
		try {
			await studioApi.applyDesignCandidate(project.id, candidateId, project.pptd.revision);
			await studio.refreshProject();
			setNotice('页面设计稿已应用，请重新导出以获取最新文件。');
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : '应用设计稿失败。');
		}
	}
	async function discardDesignCandidate(candidateId: string) {
		try {
			await studioApi.discardDesignCandidate(project.id, candidateId);
			setNotice('页面设计稿已放弃，当前文档未改变。');
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : '放弃设计稿失败。');
		}
	}
	async function readReference(event: ChangeEvent<HTMLInputElement>) {
		const file = event.target.files?.[0];
		if (!file) return;
		setReadingReference(true);
		try {
			const budget = referenceBudget ?? (await studioApi.referenceBudget());
			if (!referenceBudget) setReferenceBudget(budget);
			const document = await readReferenceDocument(file, budget.maxFileBytes);
			const existing = referenceAttachments.find((item) => item.name === file.name);
			if (referenceAttachments.length >= 5 && !existing)
				throw new Error('单个演示最多添加 5 份文本参考资料。');
			let baseReferences = preferences.references;
			if (existing) {
				const oldText =
					existing.text ??
					(await studioApi.referenceFileText(project.id, existing.id)).text;
				baseReferences = removeReferenceSection(baseReferences, existing.name, oldText);
			}
			const staged = {
				name: file.name,
				text: document.text,
				characters: document.text.length,
			};
			const nextWithStaged = [
				...referenceAttachments.filter((item) => item.name !== file.name),
				staged,
			];
			const references = composeReferenceText(baseReferences, nextWithStaged);
			if (references.length > budget.maxCharacters)
				throw new Error(
					`当前模型上下文为 ${budget.contextTokens.toLocaleString('zh-CN')} tokens，` +
						`参考资料上限为 ${formatCharacters(budget.maxCharacters)}；` +
						`本次合并后为 ${formatCharacters(references.length)}，请精简后重试。`,
				);
			const uploaded = await studioApi.uploadReferenceFile(project.id, file, document.text);
			try {
				await studio.saveReferences(references);
			} catch (cause) {
				await studioApi.deleteReferenceFile(project.id, uploaded.id).catch(() => undefined);
				throw cause;
			}
			if (existing) {
				await studioApi.deleteReferenceFile(project.id, existing.id).catch(() => undefined);
			}
			const attachment = { ...uploaded, text: document.text };
			setReferenceAttachments([
				...referenceAttachments.filter((item) => item.name !== file.name),
				attachment,
			]);
			update('references', references);
			setNotice(
				`已添加参考资料：${file.name}（${formatCharacters(document.text.length)}）${
					document.warnings.length
						? `（DOCX 解析完成，Mammoth 返回 ${document.warnings.length} 条格式提示）`
						: ''
				}`,
			);
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '无法读取资料。');
		} finally {
			setReadingReference(false);
			event.target.value = '';
		}
	}
	async function removeReferenceAttachment(name: string) {
		const attachment = referenceAttachments.find((item) => item.name === name);
		if (!attachment) return;
		const nextAttachments = referenceAttachments.filter((item) => item.name !== name);
		setReadingReference(true);
		try {
			const text =
				attachment.text ??
				(await studioApi.referenceFileText(project.id, attachment.id)).text;
			const references = removeReferenceSection(
				preferences.references,
				attachment.name,
				text,
			);
			await studio.saveReferences(references);
			await studioApi.deleteReferenceFile(project.id, attachment.id);
			update('references', references);
			setReferenceAttachments(nextAttachments);
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '无法更新参考资料。');
		} finally {
			setReadingReference(false);
		}
	}
	async function previewReference(attachment: ReferenceAttachment) {
		setPreviewingReference(true);
		try {
			const text =
				attachment.text ??
				(await studioApi.referenceFileText(project.id, attachment.id)).text;
			setReferencePreview({
				name: attachment.name,
				text,
				downloadUrl: attachment.downloadUrl,
			});
		} catch (cause) {
			studio.setError(cause instanceof Error ? cause.message : '无法读取参考资料正文。');
		} finally {
			setPreviewingReference(false);
		}
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
			<StudioHeader
				project={project}
				busy={busy}
				legacy={legacy}
				onNewProject={studio.newProject}
			/>
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
						<form onSubmit={(event) => void submit(event)}>
							<StudioCreationSettings
								project={project}
								health={health}
								connected={connected}
								busy={busy}
								legacy={legacy}
								error={studio.error}
								notice={notice}
								prompt={prompt}
								preferences={preferences}
								images={images}
								readingImages={readingImages}
								templateId={templateId}
								templates={studio.templates}
								template={template}
								inputRef={inputRef}
								templateInput={templateInput}
								imageInput={imageInput}
								onPromptChange={setPrompt}
								onPreferenceChange={update}
								onReconnect={() => void studio.reconnect()}
								onCloseError={() => studio.setError('')}
								onTemplateChange={setTemplateId}
								onUploadTemplate={(event) => void uploadTemplate(event)}
								onReadImages={(event) => void readImages(event)}
								onRemoveImage={(index) =>
									setImages((previous) =>
										previous.filter((_, item) => item !== index),
									)
								}
							/>
							<StudioMaterialsPanel
								busy={busy}
								preferences={preferences}
								onPreferenceChange={update}
								importWarnings={importWarnings}
								importWarningCount={importWarningCount}
								referenceText={referenceText}
								referenceBudget={referenceBudget}
								referenceBudgetLoading={referenceBudgetLoading}
								readingReference={readingReference}
								referenceAttachments={referenceAttachments}
								previewingReference={previewingReference}
								referenceInput={referenceInput}
								onReferenceFileChange={(event) => void readReference(event)}
								onPreviewReference={(attachment) =>
									void previewReference(attachment)
								}
								onRemoveReference={(name) => void removeReferenceAttachment(name)}
								expertMode={expertMode}
								onExpertModeChange={setExpertMode}
								hasDocument={hasDocument}
								pageIndex={pageIndex}
								scope={scope}
								onScopeChange={setScope}
							/>
							<button
								className="pptd-primary pptd-generate"
								type="submit"
								disabled={
									!canGenerate ||
									!prompt.trim() ||
									(!expertMode &&
										preferences.designMode === 'template' &&
										!templateId)
								}
							>
								<Sparkles size={17} />
								{expertMode
									? '生成演示（PPT 专家）'
									: project.pptd && preferences.workflow === 'edit'
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
										继续任务
									</button>
								)}
								{terminal && job.recoverable !== false && !checkpoint && (
									<button
										className="pptd-secondary"
										disabled={busy}
										onClick={() => void studio.retry()}
									>
										<RotateCcw size={14} />
										重新生成
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
					{job && !pageActionJob && (
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
							{liveDraft && <StudioDraftPreview draft={liveDraft} compact />}
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

							<PageActions
								pageNumber={pageIndex + 1}
								pageTitle={
									currentPage ? pageTitle(currentPage, pageIndex) : '未选择页面'
								}
								pages={pages}
								pageAction={pageAction}
								actionKey={job?.id}
								disabled={busy || !connected || !currentPage}
								runningMessage={
									job && pageActionJob && busy ? job.message : undefined
								}
								onRepair={() =>
									startPageAction(
										'page-repair',
										'检查并修复当前页面的排版、溢出和可读性问题。',
									)
								}
								onReview={() =>
									startPageAction(
										'page-review',
										'检查当前页面的视觉效果并报告具体问题。',
									)
								}
								onDesign={startPageDesign}
								onApplyCandidate={applyDesignCandidate}
								onDiscardCandidate={discardDesignCandidate}
							/>
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
							<StudioExportBar
								title={
									currentPage ? pageTitle(currentPage, pageIndex) : project.title
								}
								sourceUrl={project.pptd.sourceUrl}
								artifact={project.artifact}
								busy={busy}
								connected={connected}
								onExport={() => void studio.start('render')}
							/>
						</>
					) : legacy ? (
						<StudioLegacyPreview
							project={project}
							index={legacyIndex}
							busy={busy}
							connected={connected}
							onSelect={setLegacyIndex}
							onMigrate={() => void studio.migrate()}
						/>
					) : liveDraft ? (
						<StudioDraftPreview draft={liveDraft} />
					) : (
						<StudioEmptyState
							loading={studio.loading}
							canStop={studio.canStop}
							draft={job?.draftPreview}
							onFocus={() => inputRef.current?.focus()}
						/>
					)}
					{showVersions && (
						<StudioVersionHistory
							versions={versions}
							currentRevision={project.pptd?.revision}
							busy={busy}
							onClose={() => setShowVersions(false)}
							onRestore={(versionId) => void studio.restore(versionId)}
						/>
					)}
				</section>
			</div>
			<StudioDialogs
				referencePreview={referencePreview}
				deleteId={deleteId}
				onCloseReference={() => setReferencePreview(null)}
				onCloseDelete={() => setDeleteId(null)}
				onConfirmDelete={() => {
					const record = studio.projects.find((item) => item.id === deleteId);
					if (record) void studio.deleteProject(record);
					setDeleteId(null);
				}}
			/>
		</div>
	);
}
