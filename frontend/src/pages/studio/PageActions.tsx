import { Check, Eye, LoaderCircle, Pencil, RefreshCw, Sparkles, X } from 'lucide-react';
import { useState } from 'react';
import type { FormEvent } from 'react';

import type { PageActionReport } from './api';
import type { PptdPage } from './model';

type Props = {
	pageNumber: number;
	pageTitle: string;
	pages: PptdPage[];
	pageAction?: PageActionReport;
	actionKey?: string;
	disabled: boolean;
	runningMessage?: string;
	onRepair: () => Promise<unknown>;
	onReview: () => Promise<unknown>;
	onDesign: (prompt: string) => Promise<boolean>;
	onApplyCandidate: (candidateId: string) => Promise<unknown>;
	onDiscardCandidate: (candidateId: string) => Promise<unknown>;
};

export function PageActions({
	pageNumber,
	pageTitle,
	pages,
	pageAction,
	actionKey = '',
	disabled,
	runningMessage,
	onRepair,
	onReview,
	onDesign,
	onApplyCandidate,
	onDiscardCandidate,
}: Props) {
	const [designOpen, setDesignOpen] = useState(false);
	const [designPrompt, setDesignPrompt] = useState('');
	const [dismissed, setDismissed] = useState('');
	const [candidateBusy, setCandidateBusy] = useState<'apply' | 'discard' | null>(null);
	const showResult = !!pageAction && dismissed !== actionKey;

	async function submitDesign(event: FormEvent) {
		event.preventDefault();
		if (!designPrompt.trim() || disabled) return;
		const started = await onDesign(designPrompt.trim());
		if (started) {
			setDesignPrompt('');
			setDesignOpen(false);
		}
	}

	async function runCandidate(action: 'apply' | 'discard') {
		const candidateId = pageAction?.candidate?.id;
		if (!candidateId || candidateBusy) return;
		setCandidateBusy(action);
		try {
			if (action === 'apply') await onApplyCandidate(candidateId);
			else await onDiscardCandidate(candidateId);
			setDismissed(actionKey);
		} finally {
			setCandidateBusy(null);
		}
	}

	const pageIndex = Math.max(
		0,
		pages.findIndex((page) => page.path === pageAction?.pagePath),
	);

	return (
		<>
			<div className="pptd-page-actions" aria-label="当前页面操作">
				<div>
					<span>当前页</span>
					<strong>
						{String(pageNumber).padStart(2, '0')} {pageTitle}
					</strong>
				</div>
				<div>
					<button type="button" disabled={disabled} onClick={() => void onRepair()}>
						<RefreshCw size={15} />
						页面修复
					</button>
					<button type="button" disabled={disabled} onClick={() => void onReview()}>
						<Eye size={15} />
						AI 视觉检视
					</button>
					<button type="button" disabled={disabled} onClick={() => setDesignOpen(true)}>
						<Pencil size={15} />
						设计
					</button>
				</div>
			</div>
			{runningMessage && (
				<div className="pptd-page-action-progress" aria-live="polite">
					<LoaderCircle className="pptd-spin" size={16} />
					<span>{runningMessage}</span>
				</div>
			)}
			{showResult && pageAction && (
				<section className="pptd-page-action-result" aria-live="polite">
					<header>
						<div>
							<strong>
								{pageAction.action === 'review'
									? 'AI 视觉检视'
									: pageAction.action === 'repair'
										? '页面修复'
										: '页面设计'}
								· 第 {pageIndex + 1} 页
							</strong>
							<small>
								{pageAction.status === 'passed'
									? '检查通过，未发现阻塞问题'
									: `发现 ${pageAction.issues.length} 项问题`}
								{pageAction.revision ? ` · 版本 ${pageAction.revision}` : ''}
							</small>
						</div>
						<button
							type="button"
							aria-label="关闭页面检查结果"
							onClick={() => setDismissed(actionKey)}
						>
							<X size={15} />
						</button>
					</header>
					{!!pageAction.issues.length && (
						<ol>
							{pageAction.issues.map((issue, index) => (
								<li key={`${issue.code}-${issue.elementId ?? index}`}>
									<strong>{issue.message}</strong>
									{issue.evidence && <p>依据：{issue.evidence}</p>}
									{issue.repair && <p>建议：{issue.repair}</p>}
								</li>
							))}
						</ol>
					)}
					{pageAction.candidate && (
						<div className="pptd-design-candidate">
							<img src={pageAction.candidate.previewUrl} alt="页面设计候选稿预览" />
							<div>
								<p>
									设计稿尚未应用。确认后会保存为新版本，当前文档在确认前保持不变。
								</p>
								<button
									type="button"
									className="pptd-secondary"
									disabled={!!candidateBusy}
									onClick={() => void runCandidate('discard')}
								>
									{candidateBusy === 'discard' ? (
										<LoaderCircle className="pptd-spin" size={14} />
									) : (
										<X size={14} />
									)}
									放弃
								</button>
								<button
									type="button"
									className="pptd-primary"
									disabled={!!candidateBusy}
									onClick={() => void runCandidate('apply')}
								>
									{candidateBusy === 'apply' ? (
										<LoaderCircle className="pptd-spin" size={14} />
									) : (
										<Check size={14} />
									)}
									应用设计
								</button>
							</div>
						</div>
					)}
				</section>
			)}
			{designOpen && (
				<div
					className="pptd-design-backdrop"
					role="presentation"
					onMouseDown={(event) => {
						if (event.target === event.currentTarget) setDesignOpen(false);
					}}
				>
					<form
						className="pptd-design-dialog"
						onSubmit={(event) => void submitDesign(event)}
					>
						<header>
							<div>
								<strong>重新设计当前页</strong>
								<small>
									{String(pageNumber).padStart(2, '0')} {pageTitle}
								</small>
							</div>
							<button
								type="button"
								aria-label="关闭页面设计"
								onClick={() => setDesignOpen(false)}
							>
								<X size={16} />
							</button>
						</header>
						<label>
							<span>设计想法</span>
							<textarea
								autoFocus
								rows={4}
								value={designPrompt}
								onChange={(event) => setDesignPrompt(event.target.value)}
								placeholder="例如：改成左右双栏，左侧放核心结论，右侧用三步流程表达实施路径。"
							/>
						</label>
						<p>
							将参考当前模板和本页内容重新编排。完成后保存为新版本，可从版本记录恢复。
						</p>
						<footer>
							<button
								type="button"
								className="pptd-secondary"
								onClick={() => setDesignOpen(false)}
							>
								取消
							</button>
							<button
								type="submit"
								className="pptd-primary"
								disabled={disabled || !designPrompt.trim()}
							>
								{disabled ? (
									<LoaderCircle className="pptd-spin" size={15} />
								) : (
									<Sparkles size={15} />
								)}
								生成设计稿
							</button>
						</footer>
					</form>
				</div>
			)}
		</>
	);
}
