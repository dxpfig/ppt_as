import type { Slide, Theme } from './model';
import { slideLayoutNames } from './model';

export function SlideDraft({
	slide,
	index,
	total,
	theme,
}: {
	slide: Slide;
	index: number;
	total: number;
	theme: Theme;
}) {
	const hasImage = !!slide.image && !slide.imageDisabled;
	const layout =
		slide.layout && slide.layout !== 'auto'
			? slide.layout
			: index === 0 && total > 1
				? 'cover'
				: hasImage
					? 'image'
					: 'editorial';
	return (
		<div className={`slide-canvas designed-draft theme-${theme} draft-${layout}`}>
			<div className="draft-masthead">
				<span>{slide.section || slideLayoutNames[layout]}</span>
				<span>
					{String(index + 1).padStart(2, '0')} / {String(total).padStart(2, '0')}
				</span>
			</div>
			<h2>{slide.title}</h2>
			<div className={`draft-content ${hasImage ? 'has-picture' : ''}`}>
				<p className="draft-subtitle">{slide.subtitle}</p>
				<ol>
					{slide.points.map((point, i) => (
						<li key={i}>
							<span>{String(i + 1).padStart(2, '0')}</span>
							<strong>{point}</strong>
						</li>
					))}
				</ol>
				{hasImage && (
					<img
						src={`/api/studio/images/${slide.image!.assetId}`}
						alt={slide.image!.title}
					/>
				)}
			</div>
			<div className="draft-footer">{slide.source || '内容草稿 · 生成后查看实际排版'}</div>
		</div>
	);
}
