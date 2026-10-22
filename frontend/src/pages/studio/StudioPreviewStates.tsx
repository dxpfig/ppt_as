import { ArrowUpRight, Layers, LoaderCircle, X } from 'lucide-react';
import { useEffect, useState } from 'react';

import type { StudioJob } from './api';
import type { PptdVersion, Project } from './model';
import { SlideDraft } from './SlideDraft';

export function StudioLegacyPreview({
	project,
	index,
	busy,
	connected,
	onSelect,
	onMigrate,
}: {
	project: Project;
	index: number;
	busy: boolean;
	connected: boolean;
	onSelect: (index: number) => void;
	onMigrate: () => void;
}) {
	return (
		<div className="pptd-legacy">
			<span className="pptd-eyebrow">旧版工作记录</span>
			<h2>{project.title}</h2>
			<p>此记录保存了旧版页面草稿。迁移后即可使用新的预览、元素编辑和导出。</p>
			<button className="pptd-primary" disabled={busy || !connected} onClick={onMigrate}>
				<Layers size={16} />
				迁移为可编辑演示
			</button>
			<label className="pptd-field">
				<span>旧版页面</span>
				<select value={index} onChange={(event) => onSelect(Number(event.target.value))}>
					{project.slides.map((slide, slideIndex) => (
						<option key={slide.id} value={slideIndex}>
							{slideIndex + 1}. {slide.title}
						</option>
					))}
				</select>
			</label>
			<div className="pptd-legacy-preview">
				{project.artifact?.previewUrls[index] ? (
					<img src={project.artifact.previewUrls[index]} alt={`旧版第 ${index + 1} 页`} />
				) : (
					project.slides[index] && (
						<SlideDraft
							slide={project.slides[index]}
							index={index}
							total={project.slides.length}
							theme={project.theme}
						/>
					)
				)}
			</div>
			<small>旧版预览 · 迁移前的内容</small>
		</div>
	);
}

export function StudioEmptyState({
	loading,
	canStop,
	draft,
	onFocus,
}: {
	loading: boolean;
	canStop: boolean;
	draft?: StudioJob['draftPreview'];
	onFocus: () => void;
}) {
	return (
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
				{loading
					? '正在打开工作台…'
					: canStop
						? '正在构建你的演示'
						: '每个好想法，都值得被看见'}
			</h2>
			<p>
				{canStop
					? '页面生成后会在这里展示，保留文字、图形和图表的编辑能力。'
					: '从左侧输入主题开始，或从工作记录中继续创作。'}
			</p>
			{draft?.pages.length ? (
				<div className="pptd-draft-preview" aria-label="页面草稿预览">
					<div>
						<strong>草稿预览</strong>
						<span>
							{draft.completedPages}/{draft.totalPages} 页已保存
						</span>
					</div>
					<ol className="pptd-draft-pages">
						{draft.pages.map((page) => (
							<li
								className={`pptd-draft-page is-${page.status}`}
								key={`${page.index}-${page.path}`}
							>
								<small>{page.index + 1}</small>
								<span>{page.title}</span>
							</li>
						))}
					</ol>
				</div>
			) : null}
			{!canStop && !loading && (
				<button className="pptd-secondary" onClick={onFocus}>
					写下第一个想法
					<ArrowUpRight size={14} />
				</button>
			)}
		</div>
	);
}

export function StudioDraftPreview({
	draft,
	compact = false,
}: {
	draft: NonNullable<StudioJob['draftPreview']>;
	compact?: boolean;
}) {
	const readyPages = draft.pages.filter((page) => page.status === 'ready' && page.previewUrl);
	const latest = readyPages.at(-1);
	const [selectedPath, setSelectedPath] = useState(latest?.path ?? '');
	useEffect(() => {
		if (!latest) return;
		setSelectedPath((current) =>
			readyPages.some((page) => page.path === current) ? current : latest.path,
		);
	}, [latest, readyPages]);
	const selected =
		readyPages.find((page) => page.path === selectedPath) ?? latest ?? draft.pages[0];
	const rendering = draft.pages.find((page) => page.status === 'rendering');
	return (
		<section
			className={`pptd-live-draft ${compact ? 'is-compact' : ''}`}
			aria-label="逐页生成预览"
		>
			<header>
				<div>
					<strong>逐页实际预览</strong>
					<span>
						{draft.completedPages}/{draft.totalPages} 页可浏览
						{rendering ? ` · 正在渲染第 ${rendering.index + 1} 页` : ''}
					</span>
				</div>
				{selected?.previewUrl && (
					<a href={selected.previewUrl} target="_blank" rel="noreferrer">
						打开原图
						<ArrowUpRight size={13} />
					</a>
				)}
			</header>
			<div className="pptd-live-draft-stage">
				{selected?.previewUrl ? (
					<img
						src={selected.previewUrl}
						alt={`第 ${selected.index + 1} 页实际渲染预览`}
					/>
				) : (
					<div className="pptd-live-draft-waiting">
						<LoaderCircle size={24} className="pptd-spin" />
						<span>正在生成并渲染第一页…</span>
					</div>
				)}
				{selected && (
					<div className="pptd-live-draft-caption">
						<strong>
							{String(selected.index + 1).padStart(2, '0')} · {selected.title}
						</strong>
						<span>
							{selected.status === 'ready'
								? '浏览器实际渲染'
								: selected.status === 'rendering'
									? '正在渲染'
									: selected.status === 'failed'
										? selected.error || '预览失败'
										: '等待生成'}
						</span>
					</div>
				)}
			</div>
			<nav className="pptd-live-draft-pages" aria-label="已生成页面预览">
				{draft.pages.map((page) => (
					<button
						type="button"
						key={page.path}
						className={selected?.path === page.path ? 'is-active' : ''}
						onClick={() => page.previewUrl && setSelectedPath(page.path)}
						disabled={!page.previewUrl}
						aria-label={`第 ${page.index + 1} 页 ${page.title}`}
					>
						{page.previewUrl ? (
							<img src={page.previewUrl} alt="" loading="lazy" />
						) : (
							<span>
								{page.status === 'rendering' ? (
									<LoaderCircle size={16} className="pptd-spin" />
								) : (
									page.index + 1
								)}
							</span>
						)}
						<small>{page.index + 1}</small>
					</button>
				))}
			</nav>
		</section>
	);
}

export function StudioVersionHistory({
	versions,
	currentRevision,
	busy,
	onClose,
	onRestore,
}: {
	versions: PptdVersion[];
	currentRevision?: number;
	busy: boolean;
	onClose: () => void;
	onRestore: (versionId: string) => void;
}) {
	return (
		<aside className="pptd-versions" aria-label="版本记录">
			<div>
				<strong>版本记录</strong>
				<button className="studio-icon" aria-label="关闭版本记录" onClick={onClose}>
					<X size={16} />
				</button>
			</div>
			<p>恢复会创建新版本，保留现有历史。</p>
			{[...versions].reverse().map((version) => (
				<article key={version.id}>
					<div>
						<strong>{version.label || `版本 ${version.revision}`}</strong>
						<small>
							版本 {version.revision}
							{version.createdAt
								? ` · ${new Date(version.createdAt).toLocaleString('zh-CN')}`
								: ''}
						</small>
					</div>
					<button
						className="pptd-secondary"
						disabled={busy || version.revision === currentRevision}
						onClick={() => onRestore(version.id)}
					>
						{version.revision === currentRevision ? '当前' : '恢复'}
					</button>
				</article>
			))}
			{!versions.length && <p>版本记录正在加载。</p>}
		</aside>
	);
}
