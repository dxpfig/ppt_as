import { Check, ChevronRight, Download, Eye, FileText, Upload, X } from 'lucide-react';
import type { ChangeEvent, RefObject } from 'react';

import type { ReferenceBudget, ReferenceFile } from './api';
import type { GenerationPreferences } from './pptdState';
import { REFERENCE_FILE_ACCEPT, REFERENCE_FILE_MAX_BYTES } from './referenceDocument';
import { formatBudgetSource, formatCharacters } from './studioFormat';

type WarningGroup = { message: string; count: number };

type Props = {
	busy: boolean;
	preferences: GenerationPreferences;
	onPreferenceChange: <K extends keyof GenerationPreferences>(
		key: K,
		value: GenerationPreferences[K],
	) => void;
	importWarnings: WarningGroup[];
	importWarningCount: number;
	referenceText: string;
	referenceBudget: ReferenceBudget | null;
	referenceBudgetLoading: boolean;
	readingReference: boolean;
	referenceAttachments: ReferenceFile[];
	previewingReference: boolean;
	referenceInput: RefObject<HTMLInputElement | null>;
	onReferenceFileChange: (event: ChangeEvent<HTMLInputElement>) => void;
	onPreviewReference: (attachment: ReferenceFile) => void;
	onRemoveReference: (name: string) => void;
	expertMode: boolean;
	onExpertModeChange: (enabled: boolean) => void;
	hasDocument: boolean;
	pageIndex: number;
	scope: 'page' | 'deck';
	onScopeChange: (scope: 'page' | 'deck') => void;
};

export function StudioMaterialsPanel({
	busy,
	preferences,
	onPreferenceChange,
	importWarnings,
	importWarningCount,
	referenceText,
	referenceBudget,
	referenceBudgetLoading,
	readingReference,
	referenceAttachments,
	previewingReference,
	referenceInput,
	onReferenceFileChange,
	onPreviewReference,
	onRemoveReference,
	expertMode,
	onExpertModeChange,
	hasDocument,
	pageIndex,
	scope,
	onScopeChange,
}: Props) {
	return (
		<>
			{importWarnings.length > 0 && (
				<details className="pptd-import-warnings">
					<summary>
						<strong>模板导入说明</strong>
						<span>
							{importWarnings.length} 类差异
							{importWarningCount > importWarnings.length
								? ` · 影响 ${importWarningCount} 处`
								: ''}
						</span>
						<ChevronRight size={14} />
					</summary>
					<div>
						<p>
							模板已可用于生成；以下能力会保留为可编辑内容，但复杂样式可能近似呈现。
						</p>
						<ul>
							{importWarnings.map((item) => (
								<li key={item.message}>
									<span>{item.message}</span>
									{item.count > 1 && <strong>×{item.count}</strong>}
								</li>
							))}
						</ul>
					</div>
				</details>
			)}
			<details className="pptd-materials">
				<summary>
					<FileText size={14} />
					参考材料与内容选项
				</summary>
				<label className="pptd-field">
					<span>
						参考资料
						{referenceBudget
							? ` · ${formatCharacters(referenceText.length)} / ${formatCharacters(referenceBudget.maxCharacters)}`
							: ''}
					</span>
					<textarea
						rows={5}
						value={preferences.references}
						disabled={busy || readingReference}
						onChange={(event) => onPreferenceChange('references', event.target.value)}
						placeholder="粘贴已有内容、大纲、数据或参考说明"
					/>
				</label>
				<button
					type="button"
					className="pptd-secondary"
					disabled={busy || readingReference || referenceBudgetLoading}
					onClick={() => referenceInput.current?.click()}
				>
					<Upload size={14} />
					{readingReference ? '正在解析资料…' : '添加文本资料'}
				</button>
				<input
					ref={referenceInput}
					type="file"
					aria-label="参考文本文件，支持 TXT、Markdown、CSV、JSON 和 DOCX"
					accept={REFERENCE_FILE_ACCEPT}
					hidden
					onChange={onReferenceFileChange}
				/>
				{referenceAttachments.length > 0 && (
					<div className="pptd-reference-files">
						{referenceAttachments.map((attachment) => (
							<article key={attachment.id}>
								<div
									className={`pptd-reference-file-icon ${
										attachment.extension === '.docx' ? 'is-word' : ''
									}`}
									aria-hidden="true"
								>
									<FileText size={18} />
									<small>
										{attachment.extension === '.docx' ? 'DOCX' : 'TEXT'}
									</small>
								</div>
								<div className="pptd-reference-file-copy">
									<strong>{attachment.name}</strong>
									<small>
										<Check size={11} />
										已添加 · {formatCharacters(attachment.characters)}
									</small>
								</div>
								<div className="pptd-reference-file-actions">
									<button
										type="button"
										className="studio-icon"
										aria-label={`查看参考资料 ${attachment.name}`}
										title="查看解析内容"
										disabled={previewingReference}
										onClick={() => onPreviewReference(attachment)}
									>
										<Eye size={14} />
									</button>
									<a
										className="studio-icon"
										href={attachment.downloadUrl}
										download={attachment.name}
										aria-label={`下载参考资料 ${attachment.name}`}
										title="下载原文件"
									>
										<Download size={14} />
									</a>
									<button
										type="button"
										className="studio-icon"
										aria-label={`移除参考资料 ${attachment.name}`}
										title="移除"
										disabled={busy || readingReference}
										onClick={() => onRemoveReference(attachment.name)}
									>
										<X size={12} />
									</button>
								</div>
							</article>
						))}
					</div>
				)}
				<p className="pptd-hint">
					{referenceBudget
						? `当前模型上下文 ${referenceBudget.contextTokens.toLocaleString('zh-CN')} tokens（${formatBudgetSource(referenceBudget.source)}），` +
							`参考资料上限 ${formatCharacters(referenceBudget.maxCharacters)}，` +
							`单文件上限 ${Math.round(referenceBudget.maxFileBytes / 1024 / 1024)} MB。`
						: referenceBudgetLoading
							? '正在读取当前模型的上下文预算…'
							: `暂时无法读取模型预算，请稍后重试。原始文件上限仍为 ${Math.round(REFERENCE_FILE_MAX_BYTES / 1024 / 1024)} MB。`}
					DOCX 会读取标题、段落、列表和表格中的正文。
				</p>
				<label className="pptd-check">
					<input
						type="checkbox"
						checked={preferences.allowResearch}
						disabled={busy}
						onChange={(event) =>
							onPreferenceChange('allowResearch', event.target.checked)
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
							onPreferenceChange('speakerNotes', event.target.checked)
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
							onPreferenceChange('animationRequested', event.target.checked)
						}
					/>
					添加演示动画
				</label>
			</details>
			<label className="pptd-check">
				<input
					type="checkbox"
					checked={expertMode}
					disabled={busy}
					onChange={(event) => onExpertModeChange(event.target.checked)}
				/>
				PPT 专家（原生工作流）
			</label>
			{hasDocument && preferences.workflow === 'edit' && (
				<label className="pptd-field">
					<span>修改范围</span>
					<select
						value={scope}
						disabled={busy}
						onChange={(event) => onScopeChange(event.target.value as 'page' | 'deck')}
					>
						<option value="page">当前第 {pageIndex + 1} 页</option>
						<option value="deck">整个演示</option>
					</select>
				</label>
			)}
		</>
	);
}
