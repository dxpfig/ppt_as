import { Download, FileArchive } from 'lucide-react';

import type { Artifact } from './model';

type Props = {
	title: string;
	sourceUrl: string;
	artifact?: Artifact;
	busy: boolean;
	connected: boolean;
	onExport: () => void;
};

export function StudioExportBar({ title, sourceUrl, artifact, busy, connected, onExport }: Props) {
	return (
		<footer className="pptd-export">
			<div>
				<strong>{title}</strong>
			</div>
			<div className="pptd-export-buttons">
				{!!artifact?.warnings.length && (
					<details className="pptd-export-warnings">
						<summary>导出说明 · {artifact.warnings.length}</summary>
						<div role="note">
							{artifact.warnings.map((warning) => (
								<p key={warning}>{warning}</p>
							))}
						</div>
					</details>
				)}
				<a href={sourceUrl} className="pptd-secondary" download>
					<FileArchive size={15} />
					源文件 ZIP
				</a>
				{artifact ? (
					<>
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
					</>
				) : (
					<button
						className="pptd-primary"
						disabled={busy || !connected}
						onClick={onExport}
					>
						<Download size={15} />
						导出 PPTX / PDF
					</button>
				)}
			</div>
		</footer>
	);
}
