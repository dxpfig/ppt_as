import { ChevronRight, Layers, Plus } from 'lucide-react';

import type { Project } from './model';
import { ThemeSwitcher } from './ThemeSwitcher';

type Props = {
	project: Project;
	busy: boolean;
	legacy: boolean;
	onNewProject: () => void;
};

export function StudioHeader({ project, busy, legacy, onNewProject }: Props) {
	return (
		<header className="studio-header">
			<a className="studio-brand" href="/" aria-label="演示助手首页">
				<span className="brand-symbol">
					<Layers size={20} />
				</span>
				<strong>
					演示助手<span>STUDIO</span>
				</strong>
			</a>
			<span className="header-divider" />
			<div className="project-breadcrumb">
				<span>我的演示</span>
				<ChevronRight size={14} />
				<strong title={project.title}>{project.title}</strong>
			</div>
			<div className="header-actions">
				<span className="saved-label">
					<span />
					{busy
						? '正在处理'
						: project.pptd
							? `已保存 · 版本 ${project.pptd.revision}`
							: legacy
								? '旧版项目'
								: '新建演示'}
				</span>
				<ThemeSwitcher />
				<button className="new-chat" disabled={busy} onClick={onNewProject}>
					<Plus size={14} />
					新建
				</button>
			</div>
		</header>
	);
}
