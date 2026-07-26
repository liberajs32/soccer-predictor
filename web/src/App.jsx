import { useEffect, useMemo, useState } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

const LEAGUES = [
  { code: 'EPL', label: 'EPL' },
  { code: 'K1', label: 'K리그1' },
  { code: 'K2', label: 'K리그2' },
  { code: 'BL1', label: '분데스리가' },
]

const COMBO_TAB = 'COMBO'
const LEAGUE_LABEL = Object.fromEntries(LEAGUES.map((l) => [l.code, l.label]))

const OUTCOME_LABEL = { H: '홈승', D: '무', A: '원정승' }

function formatDate(iso) {
  if (!iso) return '날짜 미정'
  const d = new Date(iso)
  return d.toLocaleDateString('ko-KR', { month: 'short', day: 'numeric', weekday: 'short' })
}

function ProbGrid({ home, draw, away, homeLabel, awayLabel, predicted }) {
  const cells = [
    { key: 'H', label: homeLabel || '홈', value: home },
    { key: 'D', label: '무', value: draw },
    { key: 'A', label: awayLabel || '원정', value: away },
  ]
  return (
    <div className="prob-grid">
      {cells.map((c) => (
        <div key={c.key} className={`prob-cell ${c.key.toLowerCase()}${predicted === c.key ? ' predicted' : ''}`}>
          <span className="prob-cell-label">{c.label}</span>
          <span className="prob-cell-pct">{(c.value * 100).toFixed(0)}%</span>
          <div className="prob-cell-track">
            <div className="prob-cell-fill" style={{ width: `${c.value * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}

function PredictionDetail({ item }) {
  const marketProbs = useMemo(() => {
    const { odds_home, odds_draw, odds_away } = item
    if (!odds_home || !odds_draw || !odds_away) return null
    const inv = [1 / odds_home, 1 / odds_draw, 1 / odds_away]
    const total = inv[0] + inv[1] + inv[2]
    return inv.map((x) => x / total)
  }, [item])

  const homeLabel = item.home_team
  const awayLabel = item.away_team

  return (
    <>
      <div className="model-row secondary">
        <span className="model-name">Poisson 모델</span>
      </div>
      <ProbGrid
        home={item.poisson_prob_home}
        draw={item.poisson_prob_draw}
        away={item.poisson_prob_away}
        homeLabel={homeLabel}
        awayLabel={awayLabel}
      />

      <div className="model-row secondary">
        <span className="model-name">Elo 기준</span>
      </div>
      <ProbGrid
        home={item.elo_prob_home}
        draw={item.elo_prob_draw}
        away={item.elo_prob_away}
        homeLabel={homeLabel}
        awayLabel={awayLabel}
      />

      {marketProbs && (
        <>
          <div className="model-row secondary">
            <span className="model-name">배당(시장 내재확률)</span>
            <span className="odds-caption">{item.odds_home} / {item.odds_draw} / {item.odds_away}</span>
          </div>
          <ProbGrid
            home={marketProbs[0]}
            draw={marketProbs[1]}
            away={marketProbs[2]}
            homeLabel={homeLabel}
            awayLabel={awayLabel}
          />
        </>
      )}
    </>
  )
}

function MatchCard({ match, recommended }) {
  const homeLabel = match.home_team
  const awayLabel = match.away_team

  return (
    <article className={`match-card${recommended ? ' recommended' : ''}`}>
      <header>
        <span className="round">
          {match.round || ''}
          {recommended && <span className="recommended-badge">⭐ 추천 조합</span>}
        </span>
        <span className="date">{formatDate(match.date)}</span>
      </header>
      <div className="teams">
        <span className="team home">{match.home_team}</span>
        <span className="vs">vs</span>
        <span className="team away">{match.away_team}</span>
      </div>

      <div className="model-row">
        <span className="model-name">종합 예측</span>
        <span className="predicted-outcome">{OUTCOME_LABEL[match.predicted_outcome]}</span>
      </div>
      <ProbGrid
        home={match.ensemble_prob_home}
        draw={match.ensemble_prob_draw}
        away={match.ensemble_prob_away}
        homeLabel={homeLabel}
        awayLabel={awayLabel}
        predicted={match.predicted_outcome}
      />

      <PredictionDetail item={match} />
    </article>
  )
}

const COMBO_OUTCOME_OPTIONS = [
  { value: '', label: '추천 조합' },
  { value: 'D', label: '무무 조합' },
]

function comboIntro(outcome, n) {
  if (!outcome) return `가장 가까운 라운드에서 종합 예측 확신도가 가장 높은 ${n}경기를 묶었습니다.`
  const label = OUTCOME_LABEL[outcome]
  return `가장 가까운 라운드에서 "${label}" 확률이 가장 높은 ${n}경기를 묶었습니다. 그 경기의 최선 예측이 아니어도, ${label} 확률 자체가 높은 순으로 골랐습니다.`
}

function ComboLegCard({ leg }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <article className="match-card combo-leg">
      <header>
        <span className="round">{LEAGUE_LABEL[leg.league] || leg.league} · {leg.round || ''}</span>
        <span className="date">{formatDate(leg.date)}</span>
      </header>
      <div className="teams">
        <span className="team home">{leg.home_team}</span>
        <span className="vs">vs</span>
        <span className="team away">{leg.away_team}</span>
      </div>
      <div className="model-row">
        <span className="model-name">예측</span>
        <span className="predicted-outcome">
          {OUTCOME_LABEL[leg.predicted_outcome]} · {(leg.predicted_probability * 100).toFixed(0)}%
        </span>
      </div>
      {leg.odds != null && (
        <div className="model-row secondary">
          <span className="model-name">배당</span>
          <span className="odds-caption">{leg.odds}</span>
        </div>
      )}

      <button className="combo-leg-detail-toggle" onClick={() => setExpanded((v) => !v)}>
        {expanded ? '상세 접기 ▲' : '상세 보기 (승/무/패 비율) ▼'}
      </button>
      {expanded && <PredictionDetail item={leg} />}
    </article>
  )
}

function ComboView({ combo, outcome, onOutcomeChange }) {
  return (
    <div className="combo">
      <div className="combo-outcome-tabs">
        {COMBO_OUTCOME_OPTIONS.map((o) => (
          <button
            key={o.value}
            className={o.value === outcome ? 'active' : ''}
            onClick={() => onOutcomeChange(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
      <p className="combo-intro">{comboIntro(outcome, combo.legs.length)}</p>
      <div className="combo-legs">
        {combo.legs.map((leg, i) => (
          <ComboLegCard key={i} leg={leg} />
        ))}
      </div>
      <div className="combo-result">
        <span className="combo-result-label">조합 적중 확률</span>
        <span className="combo-result-pct">{(combo.combined_probability * 100).toFixed(1)}%</span>
      </div>
      <p className="combo-caveat">
        각 경기 확률을 단순히 곱한 값이라 실제와는 차이가 있을 수 있습니다. 참고용으로만 사용하세요.
      </p>
    </div>
  )
}

function App() {
  const [league, setLeague] = useState('EPL')
  const [matches, setMatches] = useState([])
  const [combo, setCombo] = useState(null)
  const [comboOutcome, setComboOutcome] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshState, setRefreshState] = useState('idle') // idle | loading | done | error
  const [refreshMessage, setRefreshMessage] = useState('')
  const [dataVersion, setDataVersion] = useState(0)
  const [recommendedKeys, setRecommendedKeys] = useState(new Set())

  const isCombo = league === COMBO_TAB

  // Independent of the active tab, so switching leagues doesn't need a
  // refetch -- same set of picks highlights across every league's list.
  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/combo?legs=2`)
      .then((res) => (res.ok ? res.json() : { legs: [] }))
      .then((data) => {
        if (cancelled) return
        setRecommendedKeys(new Set(data.legs.map((l) => `${l.league}|${l.date}|${l.home_team}|${l.away_team}`)))
      })
      .catch(() => {
        if (!cancelled) setRecommendedKeys(new Set())
      })
    return () => {
      cancelled = true
    }
  }, [dataVersion])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    const url = isCombo
      ? `${API_BASE}/combo?legs=2${comboOutcome ? `&outcome=${comboOutcome}` : ''}`
      : `${API_BASE}/predictions?league=${league}`

    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`API ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (cancelled) return
        if (isCombo) setCombo(data)
        else setMatches(data)
        setLoading(false)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err.message)
        setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [league, isCombo, comboOutcome, dataVersion])

  const handleRefresh = () => {
    setRefreshState('loading')
    setRefreshMessage('')
    fetch(`${API_BASE}/refresh`, { method: 'POST' })
      .then(async (res) => {
        const body = await res.json()
        if (!res.ok) throw new Error(body.detail || `API ${res.status}`)
        return body
      })
      .then((body) => {
        setRefreshState('done')
        setRefreshMessage(`${body.updated_rows}건 갱신 완료`)
        setDataVersion((v) => v + 1)
      })
      .catch((err) => {
        setRefreshState('error')
        setRefreshMessage(err.message)
      })
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-top">
          <h1>축구 승무패 예측</h1>
          <button
            className="refresh-button"
            onClick={handleRefresh}
            disabled={refreshState === 'loading'}
          >
            {refreshState === 'loading' ? '갱신 중...' : '새로고침'}
          </button>
        </div>
        {refreshMessage && (
          <p className={`refresh-message ${refreshState === 'error' ? 'error' : ''}`}>{refreshMessage}</p>
        )}
        <nav className="league-tabs">
          {LEAGUES.map((l) => (
            <button
              key={l.code}
              className={l.code === league ? 'active' : ''}
              onClick={() => setLeague(l.code)}
            >
              {l.label}
            </button>
          ))}
          <button
            className={isCombo ? 'active combo-tab' : 'combo-tab'}
            onClick={() => setLeague(COMBO_TAB)}
          >
            추천 조합
          </button>
        </nav>
      </header>

      <main>
        {loading && <p className="status">불러오는 중...</p>}
        {error && <p className="status error">API 연결 실패: {error} (백엔드가 켜져 있는지 확인하세요)</p>}

        {!loading && !error && isCombo && combo && (
          <ComboView combo={combo} outcome={comboOutcome} onOutcomeChange={setComboOutcome} />
        )}

        {!loading && !error && !isCombo && matches.length === 0 && (
          <p className="status">예정된 경기가 없습니다.</p>
        )}
        {!isCombo && (
          <div className="match-list">
            {matches.map((m) => (
              <MatchCard
                key={m.id}
                match={m}
                recommended={recommendedKeys.has(`${league}|${m.date}|${m.home_team}|${m.away_team}`)}
              />
            ))}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
