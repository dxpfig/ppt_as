import { ChevronDown, Monitor, Moon, Sun } from 'lucide-react';
import { useTheme } from 'next-themes';

export function ThemeSwitcher() {
	const { theme = 'system', setTheme } = useTheme();
	const Icon = theme === 'dark' ? Moon : theme === 'light' ? Sun : Monitor;
	return (
		<label className="studio-theme-switcher" title="切换界面主题">
			<Icon size={15} aria-hidden="true" />
			<select
				aria-label="界面主题"
				value={theme}
				onChange={(event) => setTheme(event.target.value)}
			>
				<option value="light">浅色</option>
				<option value="dark">深色</option>
				<option value="system">跟随系统</option>
			</select>
			<ChevronDown size={12} aria-hidden="true" />
		</label>
	);
}
