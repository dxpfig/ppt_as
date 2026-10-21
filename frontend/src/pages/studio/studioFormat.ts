export function formatCharacters(value: number) {
	if (value >= 10000) return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)} 万字`;
	return `${value.toLocaleString('zh-CN')} 字`;
}

export function formatBudgetSource(source: string) {
	if (source.includes('model-api')) return '服务端发现';
	if (source.includes('fallback')) return '配置回退预算，未获服务端确认';
	return '配置预算';
}
