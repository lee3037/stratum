import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react"
import { ChevronRight, LoaderCircle, Plus, Search } from "lucide-react"

import type { FastgptDataset, KbSummary } from "@/lib/library"
import { createKb, fastgptStatus, kbFromDataset, listDatasets, listKbs } from "@/lib/library"
import { cn } from "@/lib/utils"
import { Logo } from "@/components/logo"
import { SplitTextReveal } from "@/components/motion/split-text-reveal"
import { Button } from "@/components/ui/button"
import { EmptyAction, EmptyPanel, EmptyState, IconFolder, IconPlus } from "@/components/spectrumui/blocks/empty-states/empty-state-kit"
import { toast } from "@/components/toaster"

function ago(t: number) {
  const m = Math.round((Date.now() - t * 1000) / 60000)
  if (m < 1) return "刚刚"
  if (m < 60) return `${m} 分钟前`
  if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`
  return `${Math.round(m / 60 / 24)} 天前`
}

const fail = (title: string, e: unknown) =>
  toast({ title, description: e instanceof Error ? e.message : String(e), status: "error" })

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** 列表超过这个数量时显示搜索框 */
const SEARCH_THRESHOLD = 6

interface FastgptInfo {
  configured: boolean
  datasets: FastgptDataset[]
  error: string | null
}

/**
 * 首页：服务器上的知识库，以及 FastGPT 中还没有对应 Stratum 知识库的知识库。
 * 点击已有知识库直接进入；点击 FastGPT 知识库会新建同名知识库、完成绑定后进入。
 */
export function KbPicker({ onOpen }: { onOpen: (name: string) => Promise<void> }) {
  const [kbs, setKbs] = useState<KbSummary[] | null>(null)
  const [fastgpt, setFastgpt] = useState<FastgptInfo>({ configured: false, datasets: [], error: null })
  const [storageError, setStorageError] = useState<string | null>(null)
  const [chineseName, setChineseName] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState("")
  const [query, setQuery] = useState("")
  /** 正在打开或创建的条目（知识库名或 FastGPT 知识库 id） */
  const [pendingId, setPendingId] = useState<string | null>(null)
  const busy = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    listKbs().then(
      (r) => {
        setKbs(r.kbs)
        setStorageError(r.storage.writable ? null : `服务器文件库目录不可写：${r.storage.path}`)
      },
      (e) => {
        setKbs([])
        fail("无法读取知识库列表", e)
      },
    )
    fastgptStatus()
      .then(async (s) => {
        if (!s.configured) return
        setFastgpt({ configured: true, datasets: [], error: null })
        try {
          setFastgpt({ configured: true, datasets: await listDatasets(), error: null })
        } catch (e) {
          setFastgpt({ configured: true, datasets: [], error: message(e) })
        }
      })
      .catch(() => { /* 读不到状态时按未配置处理 */ })
  }, [])

  const list = kbs ?? []
  const loaded = kbs !== null
  const bound = useMemo(() => new Set(list.map((kb) => kb.fastgpt?.dataset_id).filter(Boolean)), [list])
  const unbound = fastgpt.datasets.filter((ds) => !bound.has(ds.id))
  const total = list.length + unbound.length
  const formOpen = creating || (loaded && total === 0)

  useEffect(() => {
    if (creating) inputRef.current?.focus()
  }, [creating])

  const q = query.trim().toLowerCase()
  const shownKbs = q
    ? list.filter((kb) => kb.name.toLowerCase().includes(q) || kb.fastgpt?.dataset_name?.toLowerCase().includes(q))
    : list
  const shownDatasets = q ? unbound.filter((ds) => ds.name.toLowerCase().includes(q)) : unbound

  const run = async (id: string, task: () => Promise<void>) => {
    if (busy.current) return
    busy.current = true
    setPendingId(id)
    try {
      await task()
    } finally {
      busy.current = false
      setPendingId(null)
    }
  }

  const open = (kb: string) => run(kb, () => onOpen(kb).catch((e) => fail("无法打开知识库", e)))

  const openDataset = (ds: FastgptDataset) =>
    run(ds.id, async () => {
      try {
        const { name: kb } = await kbFromDataset(ds.id)
        await onOpen(kb)
      } catch (e) {
        fail(`无法为「${ds.name}」创建知识库`, e)
      }
    })

  const create = (e: FormEvent) => {
    e.preventDefault()
    const value = name.trim()
    if (!value) return
    void run("", async () => {
      try {
        const { name: created } = await createKb(value)
        await onOpen(created)
      } catch (err) {
        fail("新建知识库失败", err)
      }
    })
  }

  const meta = !loaded
    ? "加载中"
    : unbound.length
      ? `${list.length} 个知识库 · FastGPT 中另有 ${unbound.length} 个可直接创建`
      : `共 ${list.length} 个知识库`

  return (
    <div className="flex min-h-svh flex-col items-center bg-muted/30 px-4 pt-14 pb-10">
      <h1>
        {/* 点击品牌名在中英文之间切换，每次切换重播逐字飞入 */}
        <button
          className="group flex cursor-pointer select-none flex-col items-center gap-4"
          onClick={() => setChineseName((v) => !v)}
          type="button"
        >
          <Logo className="size-20" />
          <SplitTextReveal
            className="font-bold text-[40px] text-neutral-900 leading-none tracking-tight transition-transform duration-200 group-hover:scale-[1.02] dark:text-neutral-100"
            text={chineseName ? "层析" : "Stratum"}
          />
        </button>
      </h1>
      <div className="flex w-full flex-1 flex-col items-center gap-4 py-8">
        {storageError && (
          <p className="max-w-[680px] rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-900/40 dark:bg-red-900/20 dark:text-red-300">
            {storageError}，请联系管理员检查 STRATUM_DATA_DIR 配置。
          </p>
        )}
        <EmptyPanel
          bodyClassName="p-0 sm:p-0"
          className="max-w-[680px]"
          meta={meta}
          title="知识库"
          toolbar={
            !formOpen && (
              <EmptyAction className="h-8 px-3 text-[13.5px]" emphasis="secondary" icon={<IconPlus />} onClick={() => setCreating(true)}>
                新建知识库
              </EmptyAction>
            )
          }
        >
          {formOpen && (
            <form className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3" onSubmit={create}>
              <input
                aria-label="新知识库名称"
                className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                maxLength={64}
                onChange={(e) => setName(e.target.value)}
                placeholder="新知识库名称，建议与 FastGPT 中的知识库同名"
                ref={inputRef}
                value={name}
              />
              <Button disabled={!name.trim() || pendingId !== null} type="submit">
                {pendingId === "" && <LoaderCircle className="animate-spin" />}
                创建并打开
              </Button>
              {total > 0 && (
                <Button onClick={() => { setCreating(false); setName("") }} type="button" variant="ghost">取消</Button>
              )}
            </form>
          )}

          {total > SEARCH_THRESHOLD && (
            <label className="flex items-center gap-2 border-b border-border px-4 py-2 text-sm text-muted-foreground">
              <Search className="size-4 shrink-0" />
              <input
                aria-label="搜索知识库"
                className="min-w-0 flex-1 bg-transparent py-1 text-foreground outline-none"
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索知识库"
                value={query}
              />
            </label>
          )}

          {!loaded ? (
            <p className="px-4 py-10 text-center text-sm text-muted-foreground">加载中…</p>
          ) : total === 0 ? (
            <EmptyState
              backdrop="stack"
              className="px-5 py-10"
              description={
                fastgpt.configured
                  ? "在上方输入名称新建。每个知识库对应 FastGPT 中的一个知识库，PDF 和解析结果都保存在服务器上。"
                  : "在上方输入名称新建。PDF 和解析结果都保存在服务器上；管理员配置 FastGPT 后，这里还会列出 FastGPT 中的知识库。"
              }
              icon={<IconFolder />}
              title="还没有知识库"
            />
          ) : (
            <div className="max-h-[min(60vh,560px)] overflow-y-auto py-1">
              {shownKbs.length > 0 && (
                <Group label={unbound.length ? "已创建" : undefined}>
                  {shownKbs.map((kb) => (
                    <Row
                      busy={pendingId === kb.name}
                      detail={`${kb.pdfs} 个 PDF · 已转换 ${kb.converted} · ${ago(kb.updated)}`}
                      disabled={pendingId !== null}
                      key={kb.name}
                      name={kb.name}
                      onClick={() => void open(kb.name)}
                      tag={
                        kb.fastgpt ? (
                          <span className="truncate" title={`已绑定 FastGPT 知识库「${kb.fastgpt.dataset_name ?? kb.fastgpt.dataset_id}」`}>
                            {kb.fastgpt.dataset_name && kb.fastgpt.dataset_name !== kb.name
                              ? `FastGPT · ${kb.fastgpt.dataset_name}`
                              : "已绑定 FastGPT"}
                          </span>
                        ) : fastgpt.configured ? (
                          <span className="text-amber-700 dark:text-amber-400">未绑定 FastGPT</span>
                        ) : null
                      }
                    />
                  ))}
                </Group>
              )}
              {shownDatasets.length > 0 && (
                <Group label="FastGPT 中的知识库 · 点击自动创建同名知识库并绑定">
                  {shownDatasets.map((ds) => (
                    <Row
                      busy={pendingId === ds.id}
                      detail={ds.vector_model ? `FastGPT 知识库 · ${ds.vector_model}` : "FastGPT 知识库"}
                      disabled={pendingId !== null}
                      key={ds.id}
                      muted
                      name={ds.name}
                      onClick={() => void openDataset(ds)}
                      tag={<span className="inline-flex items-center gap-1"><Plus className="size-3.5" />创建并打开</span>}
                    />
                  ))}
                </Group>
              )}
              {q && shownKbs.length + shownDatasets.length === 0 && (
                <p className="px-4 py-8 text-center text-sm text-muted-foreground">没有匹配「{query.trim()}」的知识库</p>
              )}
            </div>
          )}

          {fastgpt.error && (
            <p className="border-t border-border px-4 py-2.5 text-[13px] text-red-700 dark:text-red-300">
              无法读取 FastGPT 知识库列表：{fastgpt.error}
            </p>
          )}
        </EmptyPanel>
      </div>
    </div>
  )
}

function Group({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <section className="py-1">
      {label && <h2 className="px-4 pt-2 pb-1 text-[12.5px] font-medium text-muted-foreground">{label}</h2>}
      <ul>{children}</ul>
    </section>
  )
}

function Row({ name, detail, tag, muted, busy, disabled, onClick }: {
  name: string
  detail: string
  tag?: ReactNode
  muted?: boolean
  busy?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <li>
      <button
        className="group flex w-full cursor-pointer items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none disabled:cursor-default"
        disabled={disabled}
        onClick={onClick}
        type="button"
      >
        <span
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-xl border border-border [&_svg]:size-[18px]",
            muted ? "border-dashed text-muted-foreground" : "bg-background text-foreground",
          )}
        >
          {busy ? <LoaderCircle className="animate-spin" /> : <IconFolder />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{name}</span>
          <span className="block truncate text-[12.5px] text-muted-foreground">{detail}</span>
        </span>
        {tag && <span className="hidden max-w-[40%] shrink-0 text-[12.5px] text-muted-foreground sm:inline-flex">{tag}</span>}
        <ChevronRight className="size-4 shrink-0 text-muted-foreground/60 transition-transform group-hover:translate-x-0.5" />
      </button>
    </li>
  )
}
