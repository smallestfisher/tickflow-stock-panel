/**
 * 市场资讯页 —— Tab 切换四类: 快讯 / 个股研报 / 行业研报 / 公告。
 *  - 快讯: GET /api/news/telegraph, 定时刷新 (后台轮询落库)。
 *  - 个股研报/公告: 输入代码实时查询。
 *  - 行业研报: 可选行业代码, 默认全行业。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Newspaper, FileSearch, Landmark, Megaphone, RefreshCw, ExternalLink } from 'lucide-react'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'

type Tab = 'telegraph' | 'report' | 'industry' | 'notice'

const TABS: { key: Tab; label: string; icon: typeof Newspaper }[] = [
  { key: 'telegraph', label: '快讯', icon: Newspaper },
  { key: 'report', label: '个股研报', icon: FileSearch },
  { key: 'industry', label: '行业研报', icon: Landmark },
  { key: 'notice', label: '公告', icon: Megaphone },
]

export function News() {
  const [tab, setTab] = useState<Tab>('telegraph')

  return (
    <div className="p-6 space-y-4">
      <PageHeader title="市场资讯" subtitle="快讯 · 研报 · 公告" />

      <div className="flex gap-2 border-b border-border">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={cn(
              'flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 -mb-px transition-colors',
              tab === key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted hover:text-foreground',
            )}
          >
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'telegraph' && <TelegraphTab />}
      {tab === 'report' && <StockReportTab />}
      {tab === 'industry' && <IndustryReportTab />}
      {tab === 'notice' && <NoticeTab />}
    </div>
  )
}

function TelegraphTab() {
  const { data, isFetching, refetch } = useQuery({
    queryKey: QK.newsTelegraph(''),
    queryFn: () => api.newsTelegraph('', 50),
    refetchInterval: 60_000,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <button onClick={() => refetch()} className="flex items-center gap-1 text-sm text-muted hover:text-foreground">
          <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} /> 刷新
        </button>
      </div>
      {items.length === 0 && (
        <div className="text-muted text-sm py-8 text-center">
          暂无快讯。请在「设置」开启快讯轮询后等待抓取。
        </div>
      )}
      {items.map((it) => (
        <div key={it.id} className="rounded-lg border border-border p-3 space-y-1">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className={it.is_red ? 'text-bear font-semibold' : ''}>{it.time}</span>
            <span>·</span>
            <span>{it.source}</span>
            {it.url && (
              <a href={it.url} target="_blank" rel="noreferrer" className="ml-auto hover:text-primary">
                <ExternalLink size={13} />
              </a>
            )}
          </div>
          <div className={cn('text-sm leading-relaxed', it.is_red && 'text-bear')}>{it.content}</div>
          {it.subjects.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {it.subjects.map((s) => (
                <span key={s} className="text-xs px-1.5 py-0.5 rounded bg-muted/10 text-muted">
                  {s}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function StockReportTab() {
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsStockReport(query),
    queryFn: () => api.newsStockReport(query),
    enabled: query.length > 0,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={code}
        onChange={setCode}
        onSubmit={() => setQuery(code.trim())}
        placeholder="输入股票代码, 如 600519"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {query && !isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到研报。</div>
      )}
      {items.map((it, i) => (
        <ReportCard key={i} report={it} />
      ))}
    </div>
  )
}

function IndustryReportTab() {
  const [industry, setIndustry] = useState('')
  const [query, setQuery] = useState('__all__')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsIndustryReport(query),
    queryFn: () => api.newsIndustryReport(query === '__all__' ? '' : query),
    enabled: true,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={industry}
        onChange={setIndustry}
        onSubmit={() => setQuery(industry.trim() || '__all__')}
        placeholder="行业代码 (留空=全行业)"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {!isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到行业研报。</div>
      )}
      {items.map((it, i) => (
        <ReportCard key={i} report={it} />
      ))}
    </div>
  )
}

function NoticeTab() {
  const [code, setCode] = useState('')
  const [query, setQuery] = useState('')
  const { data, isFetching } = useQuery({
    queryKey: QK.newsStockNotice(query),
    queryFn: () => api.newsStockNotice(query),
    enabled: query.length > 0,
  })
  const items = data?.items ?? []
  return (
    <div className="space-y-3">
      <SearchBar
        value={code}
        onChange={setCode}
        onSubmit={() => setQuery(code.trim())}
        placeholder="输入股票代码, 如 600519"
      />
      {isFetching && <div className="text-muted text-sm">查询中…</div>}
      {query && !isFetching && items.length === 0 && (
        <div className="text-muted text-sm py-6 text-center">未查到公告。</div>
      )}
      {items.map((it, i) => (
        <div key={i} className="rounded-lg border border-border p-3 space-y-1">
          <div className="flex items-center gap-2">
            {it.columns.length > 0 && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-muted/10 text-muted">
                {it.columns.join(' ')}
              </span>
            )}
            <span className="text-sm font-medium flex-1">{it.title}</span>
            {it.url && (
              <a href={it.url} target="_blank" rel="noreferrer" className="hover:text-primary">
                <ExternalLink size={13} />
              </a>
            )}
          </div>
          <div className="text-xs text-muted">{it.date}{it.stocks.length > 0 ? ` · ${it.stocks.join(', ')}` : ''}</div>
        </div>
      ))}
    </div>
  )
}

function ReportCard({ report }: { report: import('@/lib/api').ResearchReport }) {
  return (
    <div className="rounded-lg border border-border p-3 space-y-1">
      <div className="flex items-center gap-2">
        {report.rating && (
          <span className="text-xs px-1.5 py-0.5 rounded bg-bear/10 text-bear">{report.rating}</span>
        )}
        <span className="text-sm font-medium flex-1">{report.title}</span>
        {report.url && (
          <a href={report.url} target="_blank" rel="noreferrer" className="hover:text-primary">
            <ExternalLink size={13} />
          </a>
        )}
      </div>
      <div className="text-xs text-muted">
        {[report.org, report.author, report.date].filter(Boolean).join(' · ')}
      </div>
    </div>
  )
}

function SearchBar({
  value, onChange, onSubmit, placeholder,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  placeholder: string
}) {
  return (
    <div className="flex gap-2">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && onSubmit()}
        placeholder={placeholder}
        className="flex-1 rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary"
      />
      <button
        onClick={onSubmit}
        className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground hover:opacity-90"
      >
        查询
      </button>
    </div>
  )
}
