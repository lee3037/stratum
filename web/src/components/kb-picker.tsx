import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react"
import { ChevronRight, LoaderCircle, Search } from "lucide-react"

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

const stats = (kb: KbSummary) => `${kb.pdfs} 个 PDF · 已转换 ${kb.converted} · ${ago(kb.updated)}`

/** 列表超过这个数量时显示搜索框 */
const SEARCH_THRESHOLD = 6

interface FastgptInfo {
  /** null：还在读取 FastGPT 状态 */
  configured: boolean | null
  datasets: FastgptDataset[] | null
  error: string | null
}

/**
 * 首页。配置了 FastGPT 时，列表就是 FastGPT 中的知识库：点击进入，第一次进入时服务端自动准备
 * 存放 PDF 与 Markdown 的目录并绑定，用户无需感知。没有配置 FastGPT（或读取失败）时，
 * 显示服务器上的知识库并允许手动新建。
 */
export function KbPicker({ onOpen }: { onOpen: (name: string) => Promise<void> }) {
  const [kbs, setKbs] = useState<KbSummary[] | null>(null)
  const [fastgpt, setFastgpt] = useState<FastgptInfo>({ configured: null, datasets: null, error: null })
  const [storageError, setStorageError] = useState<string | null>(null)
  const [chineseName, setChineseName] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState("")
  const [query, setQuery] = useState("")
  /** 正在进入的条目（知识库名或 FastGPT 知识库 id） */
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
        if (!s.configured) return setFastgpt({ configured: false, datasets: null, error: null })
        setFastgpt({ configured: true, datasets: null, error: null })
        try {
          setFastgpt({ configured: true, datasets: await listDatasets(), error: null })
        } catch (e) {
          setFastgpt({ configured: true, datasets: null, error: message(e) })
        }
      })
      .catch(() => setFastgpt({ configured: false, datasets: null, error: null }))
  }, [])

  const datasets = fastgpt.datasets
  /** 以 FastGPT 为准：配置了且读到了列表 */
  const synced = datasets !== null
  const loaded = kbs !== null && fastgpt.configured !== null && !(fastgpt.configured && !synced && !fastgpt.error)

  // 每个 FastGPT 知识库对应的服务器目录（第一个绑定它的）；其余目录归入「其他」
  const { byDataset, others } = useMemo(() => {
    const ids = new Set((datasets ?? []).map((ds) => ds.id))
    const map = new Map<string, KbSummary>()
    const rest: KbSummary[] = []
    for (const kb of kbs ?? []) {
      const id = kb.fastgpt?.dataset_id
      if (synced && id && ids.has(id) && !map.has(id)) map.set(id, kb)
      else rest.push(kb)
    }
    return { byDataset: map, others: rest }
  }, [kbs, datasets, synced])

  const total = (datasets?.length ?? 0) + others.length
  // 手动新建只在不以 FastGPT 为准时提供；以 FastGPT 为准时请在 FastGPT 中新建
  const canCreate = loaded && !synced
  const formOpen = canCreate && (creating || total === 0)

  useEffect(() => {
    if (creating) inputRef.current?.focus()
  }, [creating])

  const q = query.trim().toLowerCase()
  const match = (...names: (string | null | undefined)[]) => !q || names.some((n) => n?.toLowerCase().includes(q))
  const shownDatasets = (datasets ?? []).filter((ds) => match(ds.name, byDataset.get(ds.id)?.name))
  const shownOthers = others.filter((kb) => match(kb.name, kb.fastgpt?.dataset_name))

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

  const openDataset = (ds: FastgptDataset) => {
    const kb = byDataset.get(ds.id)
    if (kb) return open(kb.name)
    return run(ds.id, async () => {
      try {
        const { name: folder } = await kbFromDataset(ds.id)
        await onOpen(folder)
      } catch (e) {
        fail(`无法打开「${ds.name}」`, e)
      }
    })
  }

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
    : synced
      ? `与 FastGPT 同步 · 共 ${datasets?.length ?? 0} 个知识库`
      : `共 ${others.length} 个知识库`

  const otherTag = (kb: KbSummary): ReactNode => {
    if (!kb.fastgpt) return fastgpt.configured ? <span className="text-amber-700 dark:text-amber-400">未绑定 FastGPT</span> : null
    if (!synced) return <span className="truncate">FastGPT · {kb.fastgpt.dataset_name ?? kb.fastgpt.dataset_id}</span>
    return byDataset.get(kb.fastgpt.dataset_id)
      ? <span className="truncate">与「{byDataset.get(kb.fastgpt.dataset_id)!.name}」绑定同一个 FastGPT 知识库</span>
      : <span className="text-amber-700 dark:text-amber-400">FastGPT 中找不到绑定的知识库</span>
  }

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
            canCreate && !formOpen && (
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
                placeholder="新知识库名称"
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
                synced
                  ? "FastGPT 中还没有这个 API 密钥能访问的知识库。请先在 FastGPT 中新建知识库，再刷新本页。"
                  : "在上方输入名称新建。PDF 和解析结果都保存在服务器上；管理员配置 FastGPT 后，这里直接列出 FastGPT 中的知识库。"
              }
              icon={<IconFolder />}
              title="还没有知识库"
            />
          ) : (
            <div className="max-h-[min(60vh,560px)] overflow-y-auto py-1">
              {shownDatasets.length > 0 && (
                <Group>
                  {shownDatasets.map((ds) => {
                    const kb = byDataset.get(ds.id)
                    return (
                      <Row
                        busy={pendingId === ds.id || (kb !== undefined && pendingId === kb.name)}
                        detail={kb ? stats(kb) : "还没有上传 PDF"}
                        disabled={pendingId !== null}
                        key={ds.id}
                        muted={!kb}
                        name={ds.name}
                        onClick={() => void openDataset(ds)}
                        tag={ds.vector_model ?? undefined}
                      />
                    )
                  })}
                </Group>
              )}
              {shownOthers.length > 0 && (
                <Group label={synced ? "服务器上的其他知识库" : undefined}>
                  {shownOthers.map((kb) => (
                    <Row
                      busy={pendingId === kb.name}
                      detail={stats(kb)}
                      disabled={pendingId !== null}
                      key={kb.name}
                      name={kb.name}
                      onClick={() => void open(kb.name)}
                      tag={otherTag(kb)}
                    />
                  ))}
                </Group>
              )}
              {q && shownDatasets.length + shownOthers.length === 0 && (
                <p className="px-4 py-8 text-center text-sm text-muted-foreground">没有匹配「{query.trim()}」的知识库</p>
              )}
            </div>
          )}

          {loaded && (synced || fastgpt.error) && (
            <p
              className={cn(
                "border-t border-border px-4 py-2.5 text-[13px]",
                fastgpt.error ? "text-red-700 dark:text-red-300" : "text-muted-foreground",
              )}
            >
              {fastgpt.error
                ? `无法读取 FastGPT 知识库列表：${fastgpt.error}。暂时显示服务器上的知识库。`
                : "列表来自 FastGPT。需要新的知识库时，先在 FastGPT 中新建，再刷新本页。"}
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
            muted ? "text-muted-foreground" : "bg-background text-foreground",
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
