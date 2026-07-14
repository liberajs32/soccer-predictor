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

function MatchCard({ match }) {
  const marketProbs = useMemo(() => {
    const { odds_home, odds_draw, odds_away } = match
    if (!odds_home || !odds_draw || !odds_away) return null
    const inv = [1 / odds_home, 1 / odds_draw, 1 / odds_away]
    const total = inv[0] + inv[1] + inv[2]
    return inv.map((x) => x / total)
  }, [match])

  const homeLabel = match.home_team
  const awayLabel = match.away_team

  return (
    <article className="match-card">
      <header>
        <span className="round">{match.round || ''}</span>
        <span className="date">{formatDate(match.date)}</span>
      </header>
      <div className="teams">
        <span className="team home">{match.home_team}</span>
        <span className="vs">vs</span>
        <span className="team away">{match.away_team}</span>
      </div>

      {'ensemble_prob_home' in match ? (
        <>
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

          <div className="model-row secondary">
            <span className="model-name">Poisson 모델</span>
          </div>
          <ProbGrid
            home={match.poisson_prob_home}
            draw={match.poisson_prob_draw}
            away={match.poisson_prob_away}
            homeLabel={homeLabel}
            awayLabel={awayLabel}
          />

          <div className="model-row secondary">
            <span className="model-name">Elo 기준</span>
          </div>
          <ProbGrid
            home={match.elo_prob_home}
            draw={match.elo_prob_draw}
            away={match.elo_prob_away}
            homeLabel={homeLabel}
            awayLabel={awayLabel}
          />
        </>
      ) : null}

      {marketProbs && (
        <>
          <div className="model-row secondary">
            <span className="model-name">배당(시장 내재확률)</span>
            <span className="odds-caption">{match.odds_home} / {match.odds_draw} / {match.odds_away}</span>
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
    </article>
  )
}

function ComboView({ combo }) {
  return (
    <div className="combo">
      <p className="combo-intro">
        전 리그 통틀어 종합 예측 확신도가 가장 높은 {combo.legs.length}경기를 묶었습니다.
      </p>
      <div className="combo-legs">
        {combo.legs.map((leg, i) => (
          <article key={i} className="match-card combo-leg">
            <header>
              <span className="round">{LEAGUE_LABEL[leg.league] || leg.league}</span>
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
          </article>
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const isCombo = league === COMBO_TAB

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)

    const url = isCombo ? `${API_BASE}/combo?legs=2` : `${API_BASE}/predictions?league=${league}`

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
  }, [league, isCombo])

  return (
    <div className="app">
      <header className="app-header">
        <h1>축구 승무패 예측</h1>
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

        {!loading && !error && isCombo && combo && <ComboView combo={combo} />}

        {!loading && !error && !isCombo && matches.length === 0 && (
          <p className="status">예정된 경기가 없습니다.</p>
        )}
        {!isCombo && (
          <div className="match-list">
            {matches.map((m) => (
              <MatchCard key={m.id} match={m} />
            ))}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
