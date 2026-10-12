import { ImageIcon, LoaderCircle, Search, Trash2 } from 'lucide-react';
import { useState } from 'react';

import { studioApi } from './api';
import type { ImageStatus } from './api';
import type { ImageSource, Slide, SlideImage } from './model';

const providerNames: Record<string, string> = {
	weknora: 'WeKnora',
	mcp: 'MCP 搜索',
	tavily: 'Tavily',
	serper: 'Serper',
	searxng: 'SearXNG',
	bing: 'Bing 图片',
	commons: 'Wikimedia Commons',
};

export function ImageSettings({
	value,
	busy,
	status,
	onChange,
}: {
	value: ImageSource;
	busy: boolean;
	status?: ImageStatus;
	onChange: (source: ImageSource) => void;
}) {
	const [checking, setChecking] = useState(false);
	const [message, setMessage] = useState('');
	return (
		<section className="image-settings" aria-label="配图设置">
			<div className="section-label">
				<strong>
					<ImageIcon size={13} />
					内容配图
				</strong>
				<button
					disabled={checking || busy}
					onClick={() => {
						setChecking(true);
						void studioApi
							.imageStatus()
							.then((result) =>
								setMessage(
									result.warning ||
										(result.weknoraConnected
											? `WeKnora 已连接，${result.knowledgeBases?.length ?? 0} 个可访问知识库。`
											: '未配置知识库，可使用网络搜索。'),
								),
							)
							.catch((cause: unknown) =>
								setMessage(cause instanceof Error ? cause.message : '检查失败'),
							)
							.finally(() => setChecking(false));
					}}
				>
					{checking ? '检查中…' : '检查知识库连接'}
				</button>
			</div>
			<select
				aria-label="配图来源"
				value={value}
				disabled={busy}
				onChange={(event) => onChange(event.target.value as ImageSource)}
			>
				<option value="auto">自动配图 · 知识库优先，网络补充</option>
				<option value="knowledge">仅使用本地知识库</option>
				<option value="web">仅使用网络图片</option>
				<option value="none">不配图 · 纯文字版式</option>
			</select>
			<p>
				{value === 'none'
					? '已关闭本演示的内容配图。'
					: '生成时按每页内容检索配图，可在「编辑」中替换或移除。'}
			</p>
			{status && value !== 'none' && (
				<small>
					网络检索：
					{status.webProviders.map((name) => providerNames[name] ?? name).join(' → ') ||
						'未启用'}
				</small>
			)}
			{message && (
				<p className="image-search-status" role="status">
					{message}
				</p>
			)}
		</section>
	);
}

function ImageCredit({ image }: { image: SlideImage }) {
	return (
		<div className="image-credit">
			<span>
				{providerNames[image.provider] ?? image.provider} · {image.license}
			</span>
			{image.author && <span>{image.author}</span>}
			{/^https?:\/\//i.test(image.sourceUrl) && (
				<a href={image.sourceUrl} target="_blank" rel="noreferrer">
					查看原始来源 ↗
				</a>
			)}
		</div>
	);
}

export function SlideImageEditor({
	slide,
	source,
	busy,
	onChange,
}: {
	slide: Slide;
	source: ImageSource;
	busy: boolean;
	onChange: (patch: Partial<Slide>) => void;
}) {
	const [query, setQuery] = useState(slide.imageQuery || slide.title);
	const [searching, setSearching] = useState(false);
	const [candidates, setCandidates] = useState<SlideImage[]>([]);
	const [warnings, setWarnings] = useState<string[]>([]);
	return (
		<section className="slide-image-editor" aria-label="单页配图">
			<div className="section-label">
				<strong>
					<ImageIcon size={14} />
					本页配图
				</strong>
				{slide.image && (
					<button
						disabled={busy}
						onClick={() => {
							onChange({ image: null, imageDisabled: true });
							setCandidates([]);
						}}
					>
						<Trash2 size={13} />
						移除
					</button>
				)}
			</div>
			{slide.image && !slide.imageDisabled ? (
				<>
					<img
						className="selected-slide-image"
						src={`/api/studio/images/${slide.image.assetId}`}
						alt={slide.image.title}
					/>
					<ImageCredit image={slide.image} />
				</>
			) : (
				<p className="image-empty-hint">
					{slide.imageDisabled
						? '本页已关闭配图，选择新图片可重新启用。'
						: slide.imageIntent === 'none'
							? '本页以文字和结构表达，可手动添加图片。'
							: '生成时自动匹配图片，也可以先搜索并选择。'}
				</p>
			)}
			<label>
				图片关键词
				<input
					aria-label="图片关键词"
					maxLength={200}
					value={query}
					disabled={busy || searching}
					onChange={(event) => setQuery(event.target.value)}
					onBlur={() => {
						if (query.trim() !== slide.imageQuery)
							onChange({ imageQuery: query.trim() });
					}}
				/>
			</label>
			<div className="image-search-actions">
				<button
					className="secondary-button"
					disabled={busy || searching || !query.trim() || source === 'none'}
					onClick={() => {
						setSearching(true);
						setWarnings([]);
						setCandidates([]);
						if (query.trim() !== slide.imageQuery)
							onChange({ imageQuery: query.trim() });
						void studioApi
							.searchImages(query.trim(), source === 'none' ? 'auto' : source)
							.then((result) => {
								setCandidates(result.images);
								setWarnings(
									result.warnings.length
										? result.warnings
										: result.images.length
											? []
											: ['没有找到合适图片，请尝试更具体的主体或场景。'],
								);
							})
							.catch((cause: unknown) =>
								setWarnings([
									cause instanceof Error ? cause.message : '图片检索失败',
								]),
							)
							.finally(() => setSearching(false));
					}}
				>
					{searching ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
					{searching ? '正在检索图片…' : '搜索配图'}
				</button>
				{!slide.image && !slide.imageDisabled && (
					<button disabled={busy} onClick={() => onChange({ imageDisabled: true })}>
						本页不配图
					</button>
				)}
			</div>
			{source === 'none' && (
				<p className="image-empty-hint">请先在「配图来源」中启用配图。</p>
			)}
			{warnings.length > 0 && (
				<div className="image-search-status" role="status">
					{warnings.join(' ')}
				</div>
			)}
			<div className="image-candidates">
				{candidates.map((image) => (
					<article key={image.assetId}>
						<button
							disabled={busy}
							aria-label={`使用配图：${image.title}`}
							onClick={() => {
								onChange({ image, imageDisabled: false, imageQuery: query.trim() });
								setCandidates([]);
							}}
						>
							<img src={`/api/studio/images/${image.assetId}`} alt={image.title} />
							<span>{image.title}</span>
						</button>
						<ImageCredit image={image} />
					</article>
				))}
			</div>
			{slide.image && <small>应用修改后，图片将嵌入 PPTX，并同步到预览与 PDF。</small>}
		</section>
	);
}
