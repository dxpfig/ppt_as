import { Download, X } from 'lucide-react';

type ReferencePreview = {
	name: string;
	text: string;
	downloadUrl: string;
};

type Props = {
	referencePreview: ReferencePreview | null;
	deleteId: string | null;
	onCloseReference: () => void;
	onCloseDelete: () => void;
	onConfirmDelete: () => void;
};

export function StudioDialogs({
	referencePreview,
	deleteId,
	onCloseReference,
	onCloseDelete,
	onConfirmDelete,
}: Props) {
	return (
		<>
			{referencePreview && (
				<div className="pptd-modal-backdrop">
					<div
						className="pptd-modal pptd-reference-preview"
						role="dialog"
						aria-modal="true"
						aria-label={`查看参考资料 ${referencePreview.name}`}
					>
						<header>
							<div>
								<strong>{referencePreview.name}</strong>
								<small>已解析正文预览</small>
							</div>
							<button
								type="button"
								className="studio-icon"
								aria-label="关闭参考资料预览"
								onClick={onCloseReference}
							>
								<X size={15} />
							</button>
						</header>
						<pre>{referencePreview.text}</pre>
						<div>
							<a
								className="pptd-secondary"
								href={referencePreview.downloadUrl}
								download={referencePreview.name}
							>
								<Download size={14} />
								下载原文件
							</a>
							<button className="pptd-primary" onClick={onCloseReference}>
								关闭
							</button>
						</div>
					</div>
				</div>
			)}
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
							<button className="pptd-secondary" onClick={onCloseDelete}>
								取消
							</button>
							<button className="pptd-primary" onClick={onConfirmDelete}>
								删除记录
							</button>
						</div>
					</div>
				</div>
			)}
		</>
	);
}
