import { Check, RefreshCw, Upload, X } from 'lucide-react';
import type { RefObject } from 'react';

import type { DesignMode, Health, Scenario, Workflow } from './api';
import type { Project, Template } from './model';
import type { GenerationPreferences } from './pptdState';

const scenarios: { value: Scenario; label: string }[] = [
	{ value: 'auto', label: '自动识别场景' },
	{ value: 'management-report', label: '工作汇报' },
	{ value: 'analysis-decision', label: '分析决策' },
	{ value: 'business-plan', label: '商业计划' },
	{ value: 'tech-engineering', label: '技术工程' },
	{ value: 'academic-research', label: '学术研究' },
	{ value: 'education-training', label: '教育培训' },
	{ value: 'brand-creative', label: '品牌创意' },
];

type ImageReference = { name: string; imageData: string; size: number };

type Props = {
	project: Project;
	health: Health | null;
	connected: boolean;
	busy: boolean;
	legacy: boolean;
	error: string;
	notice: string;
	prompt: string;
	preferences: GenerationPreferences;
	images: ImageReference[];
	readingImages: boolean;
	templateId: string;
	templates: Template[];
	template?: Template;
	inputRef: RefObject<HTMLTextAreaElement | null>;
	templateInput: RefObject<HTMLInputElement | null>;
	imageInput: RefObject<HTMLInputElement | null>;
	onPromptChange: (value: string) => void;
	onPreferenceChange: <K extends keyof GenerationPreferences>(
		key: K,
		value: GenerationPreferences[K],
	) => void;
	onReconnect: () => void;
	onCloseError: () => void;
	onTemplateChange: (templateId: string) => void;
	onUploadTemplate: (event: React.ChangeEvent<HTMLInputElement>) => void;
	onReadImages: (event: React.ChangeEvent<HTMLInputElement>) => void;
	onRemoveImage: (index: number) => void;
};

export function StudioCreationSettings({
	project,
	health,
	connected,
	busy,
	legacy,
	error,
	notice,
	prompt,
	preferences,
	images,
	readingImages,
	templateId,
	templates,
	template,
	inputRef,
	templateInput,
	imageInput,
	onPromptChange,
	onPreferenceChange,
	onReconnect,
	onCloseError,
	onTemplateChange,
	onUploadTemplate,
	onReadImages,
	onRemoveImage,
}: Props) {
	return (
		<>
			<div className="pptd-intro">
				<h1>{project.pptd ? '让表达更进一步' : '把你的想法，变成精彩演示。'}</h1>
				<p>
					{project.pptd
						? '描述修改目标，或直接在右侧选择元素编辑。'
						: '输入主题、材料或完整大纲，自动规划内容和视觉设计。'}
				</p>
			</div>
			<div className={`pptd-model ${health?.llmConfigured ? '' : 'is-unavailable'}`}>
				<span className="pptd-status-dot" />
				<div>
					<strong>{connected ? health?.model || '读取模型配置…' : '服务未连接'}</strong>
					<small>
						{connected
							? `${health?.provider || '模型服务'} · ${health?.llmConfigured ? '已配置' : '未配置模型凭据'}`
							: '检查后端服务后重新连接'}
					</small>
				</div>
				<button
					className="studio-icon"
					title="刷新模型状态"
					aria-label="刷新模型状态"
					onClick={onReconnect}
				>
					<RefreshCw size={14} />
				</button>
			</div>
			{error && (
				<div className="pptd-error" role="alert">
					<span>{error}</span>
					<button
						className="studio-icon"
						aria-label="关闭错误提示"
						onClick={onCloseError}
					>
						<X size={14} />
					</button>
				</div>
			)}
			{notice && (
				<div className="pptd-notice" role="status">
					<Check size={14} />
					{notice}
				</div>
			)}
			<label className="pptd-field">
				<span>{project.pptd ? '修改要求' : '演示主题或内容'}</span>
				<textarea
					ref={inputRef}
					value={prompt}
					onChange={(event) => onPromptChange(event.target.value)}
					disabled={busy || legacy}
					rows={5}
					placeholder={
						project.pptd
							? '例如：让这页突出核心结论，用图表解释数据变化…'
							: '例如：面向管理团队的季度业务复盘，重点呈现增长、挑战与下一步计划…'
					}
				/>
			</label>
			<div className="pptd-field-grid">
				<label className="pptd-field">
					<span>创作方式</span>
					<select
						value={preferences.workflow}
						disabled={busy || legacy}
						onChange={(event) =>
							onPreferenceChange('workflow', event.target.value as Workflow)
						}
					>
						<option value="new">新建演示</option>
						<option value="edit" disabled={!project.pptd}>
							修改当前演示
						</option>
						<option value="recreate">参考重建</option>
					</select>
				</label>
				<label className="pptd-field">
					<span>页数</span>
					<div className="pptd-page-count">
						<select
							aria-label="页数模式"
							value={preferences.pageCount === 'auto' ? 'auto' : 'custom'}
							disabled={busy}
							onChange={(event) =>
								onPreferenceChange(
									'pageCount',
									event.target.value === 'auto'
										? 'auto'
										: String(project.pptd?.pageCount || 10),
								)
							}
						>
							<option value="auto">自动规划</option>
							<option value="custom">指定页数</option>
						</select>
						{preferences.pageCount !== 'auto' && (
							<input
								aria-label="指定页数"
								type="number"
								min={1}
								max={60}
								value={preferences.pageCount}
								onChange={(event) =>
									onPreferenceChange('pageCount', event.target.value)
								}
								disabled={busy}
							/>
						)}
					</div>
				</label>
				<label className="pptd-field">
					<span>使用场景</span>
					<select
						value={preferences.scenario}
						disabled={busy}
						onChange={(event) =>
							onPreferenceChange('scenario', event.target.value as Scenario)
						}
					>
						{scenarios.map((item) => (
							<option key={item.value} value={item.value}>
								{item.label}
							</option>
						))}
					</select>
				</label>
				<label className="pptd-field">
					<span>使用方式</span>
					<select
						value={preferences.usage}
						disabled={busy}
						onChange={(event) =>
							onPreferenceChange(
								'usage',
								event.target.value as 'reading' | 'presentation',
							)
						}
					>
						<option value="presentation">现场演讲</option>
						<option value="reading">独立阅读</option>
					</select>
				</label>
			</div>
			<label className="pptd-field">
				<span>受众</span>
				<input
					value={preferences.audience}
					disabled={busy}
					onChange={(event) => onPreferenceChange('audience', event.target.value)}
					placeholder="例如：管理团队、客户、学生"
				/>
			</label>
			<label className="pptd-field">
				<span>视觉设计</span>
				<select
					value={preferences.designMode}
					disabled={busy}
					onChange={(event) =>
						onPreferenceChange('designMode', event.target.value as DesignMode)
					}
				>
					<option value="autonomous">自主设计 · 根据内容选择</option>
					<option value="design-system">指定设计规范</option>
					<option value="template">使用公司模板</option>
					<option value="style-reference">参考视觉风格</option>
				</select>
			</label>
			{['design-system', 'style-reference'].includes(preferences.designMode) && (
				<label className="pptd-field">
					<span>
						{preferences.designMode === 'design-system' ? '设计规范' : '风格说明'}
					</span>
					<textarea
						rows={3}
						value={preferences.designSystem}
						disabled={busy}
						onChange={(event) => onPreferenceChange('designSystem', event.target.value)}
						placeholder="描述字体、配色、排版与希望参考的视觉风格"
					/>
				</label>
			)}
			{preferences.designMode === 'template' && (
				<div className="pptd-template">
					<label className="pptd-field">
						<span>公司模板</span>
						<select
							aria-label="公司模板"
							value={templateId}
							disabled={busy}
							onChange={(event) => onTemplateChange(event.target.value)}
						>
							<option value="">选择或上传模板</option>
							{templates.map((item) => (
								<option key={item.id} value={item.id}>
									{item.name}
								</option>
							))}
						</select>
					</label>
					<button
						type="button"
						className="pptd-secondary"
						disabled={busy}
						onClick={() => templateInput.current?.click()}
					>
						<Upload size={14} />
						上传 PPTX / POTX
					</button>
					<input
						ref={templateInput}
						type="file"
						accept=".pptx,.potx"
						hidden
						onChange={onUploadTemplate}
					/>
					{template?.warnings.map((warning) => (
						<p className="pptd-hint" key={warning}>
							{warning}
						</p>
					))}
				</div>
			)}
			{(preferences.workflow === 'recreate' ||
				preferences.designMode === 'style-reference' ||
				images.length > 0) && (
				<div className="pptd-reference-images">
					<span>视觉参考</span>
					<p>添加截图或设计参考图，让模型读取画面内容与风格。</p>
					<button
						type="button"
						className="pptd-secondary"
						disabled={busy || readingImages}
						onClick={() => imageInput.current?.click()}
					>
						<Upload size={14} />
						{readingImages ? '正在读取图片…' : '添加参考图片'}
					</button>
					<input
						ref={imageInput}
						aria-label="参考图片文件"
						type="file"
						accept="image/png,image/jpeg,image/webp"
						multiple
						hidden
						onChange={onReadImages}
					/>
					<div>
						{images.map((image, index) => (
							<figure key={`${image.name}-${index}`}>
								<img src={image.imageData} alt={image.name} />
								<figcaption>{image.name}</figcaption>
								<button
									type="button"
									className="studio-icon"
									aria-label={`移除参考图 ${image.name}`}
									disabled={busy || readingImages}
									onClick={() => onRemoveImage(index)}
								>
									<X size={12} />
								</button>
							</figure>
						))}
					</div>
				</div>
			)}
		</>
	);
}
