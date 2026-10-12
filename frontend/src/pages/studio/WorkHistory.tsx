import {
	FileText,
	History,
	PanelLeftClose,
	PanelLeftOpen,
	Plus,
	RefreshCw,
	Trash2,
	Undo2,
} from 'lucide-react';
import { useState } from 'react';

import type { ProjectSummary } from './api';
import type { Project } from './model';

function updatedLabel(timestamp?: number) {
	if (!timestamp) return '刚刚';
	const date = new Date(timestamp * 1000);
	return date.toLocaleString('zh-CN', {
		month: '2-digit',
		day: '2-digit',
		hour: '2-digit',
		minute: '2-digit',
	});
}

export function WorkHistory({
	projects,
	current,
	compact = false,
	busy,
	connected,
	onOpen,
	onNew,
	onDelete,
	onRefresh,
	lastDeleted,
	onUndo,
}: {
	projects: ProjectSummary[];
	current: Project;
	compact?: boolean;
	busy: boolean;
	connected: boolean;
	onOpen: (id: string) => void;
	onNew: () => void;
	onDelete: (project: ProjectSummary) => void;
	onRefresh: () => void;
	lastDeleted: ProjectSummary | null;
	onUndo: () => void;
}) {
	const [preferredCollapsed, setCollapsed] = useState<boolean | null>(null);
	const collapsed = preferredCollapsed ?? compact;
	return (
		<aside className={`work-history ${collapsed ? 'is-collapsed' : ''}`} aria-label="工作记录">
			<div className="history-heading">
				{!collapsed && (
					<strong>
						<History size={16} />
						工作记录 <small>{projects.length}</small>
					</strong>
				)}
				<button
					className="studio-icon"
					aria-label={collapsed ? '展开工作记录' : '收起工作记录'}
					title={collapsed ? '展开工作记录' : '收起工作记录'}
					aria-expanded={!collapsed}
					onClick={() => setCollapsed(!collapsed)}
				>
					{collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
				</button>
			</div>
			<button
				className="history-new"
				disabled={busy}
				onClick={onNew}
				title="新建工作记录"
				aria-label="新建工作记录"
			>
				<Plus size={17} />
				{!collapsed && '新建演示'}
			</button>
			{!collapsed && (
				<>
					<div className="history-caption">
						<span>最近编辑</span>
						<button
							className="studio-icon"
							disabled={busy}
							onClick={onRefresh}
							aria-label="刷新工作记录"
							title="刷新工作记录"
						>
							<RefreshCw size={13} />
						</button>
					</div>
					<nav className="history-list" aria-label="已保存的演示">
						{projects.length ? (
							projects.map((record) => {
								const selected = current.id === record.id;
								const title =
									(selected ? current.title : record.title) || '未命名演示';
								return (
									<div
										className={`history-item ${selected ? 'is-active' : ''}`}
										key={record.id}
									>
										<button
											className="history-open"
											disabled={busy}
											aria-current={selected ? 'page' : undefined}
											aria-label={`打开工作记录：${title}`}
											onClick={() => onOpen(record.id)}
											title={title}
										>
											<FileText size={16} />
											<span>
												<strong>{title}</strong>
												<small>
													{record.slideCount} 页 ·{' '}
													{updatedLabel(record.updatedAt)}
												</small>
											</span>
										</button>
										<button
											className="history-delete studio-icon"
											disabled={busy || !connected}
											aria-label={`删除工作记录：${title}`}
											title="删除工作记录"
											onClick={() => onDelete({ ...record, title })}
										>
											<Trash2 size={14} />
										</button>
									</div>
								);
							})
						) : (
							<div className="history-empty">
								<FileText size={24} strokeWidth={1.2} />
								<strong>还没有工作记录</strong>
								<p>
									输入需求开始创作，
									<br />
									内容会自动保存在这里。
								</p>
							</div>
						)}
					</nav>
					{lastDeleted && (
						<div className="history-undo" role="status">
							<span>已删除「{lastDeleted.title}」</span>
							<button disabled={busy} onClick={onUndo}>
								<Undo2 size={13} />
								撤销删除
							</button>
						</div>
					)}
					<p className="history-footnote">
						{busy
							? '正在处理，请稍后切换记录'
							: connected
								? '自动保存 · 点击记录继续创作'
								: '服务未连接 · 本机草稿仍保留'}
					</p>
				</>
			)}
		</aside>
	);
}
