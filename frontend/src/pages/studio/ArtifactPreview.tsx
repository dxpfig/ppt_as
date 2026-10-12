import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';

import type { Artifact } from './model';

/**
 * Preview for decks produced by the company-template route.
 *
 * Those decks are the company's own file with new words - background, logo,
 * footer, fonts and add-ins are kept byte for byte - so there is no PPTD
 * document to edit here: the published PPTX and its rendered pages are the
 * artifact. Editing stays in PowerPoint, which is the point of that route.
 */
export function ArtifactPreview({ artifact, title }: { artifact: Artifact; title: string }) {
	const [index, setIndex] = useState(0);
	const count = artifact.previewUrls.length;
	useEffect(() => {
		if (index >= count) setIndex(0);
	}, [count, index]);
	if (!count) return null;
	const current = Math.min(index, count - 1);
	return (
		<>
			<div className="pptd-preview-stage">
				<nav className="pptd-page-list" aria-label="演示页面">
					{artifact.previewUrls.map((url, position) => (
						<button
							key={url}
							aria-current={position === current ? 'page' : undefined}
							className={position === current ? 'is-active' : ''}
							onClick={() => setIndex(position)}
							aria-label={`第 ${position + 1} 页`}
						>
							<img src={url} alt="" loading="lazy" />
							<span className="pptd-page-caption">
								<span>{String(position + 1).padStart(2, '0')}</span>
							</span>
						</button>
					))}
				</nav>
				<div className="pptd-frame-wrap">
					<img
						className="pptd-artifact-page"
						src={artifact.previewUrls[current]}
						alt={`第 ${current + 1} 页`}
					/>
				</div>
			</div>
			<footer className="pptd-export">
				<div>
					<strong>{title}</strong>
					<span className="pptd-artifact-note">
						公司模板路线：沿用模板页面，只替换文字
					</span>
				</div>
				<div className="pptd-export-buttons">
					{!!artifact.warnings.length && (
						<details className="pptd-export-warnings">
							<summary>导出提示 · {artifact.warnings.length}</summary>
							<div role="status">
								{artifact.warnings.map((warning) => (
									<p key={warning}>{warning}</p>
								))}
							</div>
						</details>
					)}
					<a className="pptd-secondary" href={artifact.pptxUrl} download>
						<Download size={15} />
						PPTX
					</a>
					{artifact.pdfUrl && (
						<a className="pptd-secondary" href={artifact.pdfUrl} download>
							<Download size={15} />
							PDF
						</a>
					)}
				</div>
			</footer>
		</>
	);
}
