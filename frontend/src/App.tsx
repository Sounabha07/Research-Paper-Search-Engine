import { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { Search, BookOpen, User, Calendar, ExternalLink, Loader2, AlertCircle, Sparkles, Link } from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8080';

interface Paper {
  id: string;
  title: string;
  abstractText?: string;
  authors: string | string[];
  updateDate?: string;
}

export default function App() {

  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<Paper[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [page, setPage] = useState(1);
  const [lastQuery, setLastQuery] = useState('');

  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);

  const searchContainerRef = useRef<HTMLFormElement>(null);

  useEffect(() => {


    const handleClickOutside = (event: MouseEvent) => {
      if (
        searchContainerRef.current &&
        !searchContainerRef.current.contains(event.target as Node)
      ) {
        setShowSuggestions(false);
      }
    };

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);


  }, []);

  useEffect(() => {


    const fetchSuggestions = async () => {

      if (query.length < 2) {
        setSuggestions([]);
        return;
      }

      try {

        const response = await axios.get<string[]>(
          `${API_BASE}/autocomplete?q=${encodeURIComponent(query)}`
        );

        setSuggestions(response.data);

      } catch (err) {
        console.error("Autocomplete fetch failed", err);
      }

    };

    const timeout = setTimeout(fetchSuggestions, 300);
    return () => clearTimeout(timeout);


  }, [query]);

  const fetchResults = async (searchQuery: string, pageNum: number) => {
    if (!searchQuery.trim()) return;

    setLoading(true);
    setError(null);
    setHasSearched(true);
    setShowSuggestions(false);

    try {
      const response = await axios.get<Paper[]>(
        `${API_BASE}/search?q=${encodeURIComponent(searchQuery)}&page=${pageNum}`
      );
      setData(response.data);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err: any) {
      setError(
        err.response?.data?.message ||
        err.message ||
        "Error fetching results"
      );
      setData([]);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = async (searchQuery: string) => {
    if (!searchQuery.trim()) return;
    setQuery(searchQuery);
    setLastQuery(searchQuery);
    setPage(1);
    await fetchResults(searchQuery, 1);
  };

  // Refetch when page changes (but only after the first search)
  useEffect(() => {
    if (lastQuery) {
      fetchResults(lastQuery, page);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const handleFetchSimilar = async (paperId: string) => {


    setLoading(true);
    setError(null);
    setHasSearched(true);

    try {

      const response = await axios.get<Paper[]>(
        `${API_BASE}/similar/${paperId}`
      );

      setData(response.data);

      window.scrollTo({
        top: 0,
        behavior: 'smooth'
      });

    } catch (err: any) {

      setError(
        err.response?.data?.message ||
        err.message ||
        "Error fetching similar papers"
      );

    } finally {
      setLoading(false);
    }


  };

  const formatAuthors = (authors: string | string[]) => {


    if (!authors) return "Unknown authors";

    if (Array.isArray(authors)) {
      return authors.join(", ");
    }

    return authors;


  };

  return (


    < div className="app-container" >

      <header className="header">
        <Sparkles className="logo-icon" />
        <h1 className="title">NeuralSeek Engine</h1>
        <p className="subtitle">
          Discover state-of-the-art computer science research papers with hybrid search
        </p>
      </header>

      <form
        className="search-container"
        ref={searchContainerRef}
        style={{ position: "relative" }}
        onSubmit={(e) => {
          e.preventDefault();
          handleSearch(query);
        }}
      >

        <div className="search-input-wrapper">

          <Search className="search-icon" size={20} />

          <input
            type="text"
            className="search-input"
            placeholder="Search across research papers..."
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setShowSuggestions(true);
            }}
            onFocus={() => setShowSuggestions(true)}
          />

          <button
            type="submit"
            className="search-button"
            disabled={loading || !query.trim()}
          >
            {loading
              ? <Loader2 className="loading-spinner" size={20} />
              : "Search"}
          </button>

        </div>

        {showSuggestions && suggestions.length > 0 && (

          <div style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            marginTop: '0.5rem',
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border-color)',
            borderRadius: '16px',
            boxShadow: 'var(--card-shadow)',
            overflow: 'hidden',
            zIndex: 50
          }}>

            {suggestions.map((suggestion, idx) => (

              <div
                key={idx}
                onClick={() => handleSearch(suggestion)}
                style={{
                  padding: '1rem 1.5rem',
                  cursor: 'pointer',
                  borderBottom:
                    idx < suggestions.length - 1
                      ? '1px solid var(--border-color)'
                      : 'none',
                  color: 'var(--text-primary)',
                  transition: 'background 0.2s'
                }}
                onMouseEnter={(e) =>
                  e.currentTarget.style.background =
                  'rgba(255,255,255,0.05)'
                }
                onMouseLeave={(e) =>
                  e.currentTarget.style.background = 'transparent'
                }
              >

                <Search
                  size={14}
                  style={{ display: 'inline', marginRight: '8px', opacity: 0.5 }}
                />

                {suggestion}

              </div>

            ))}

          </div>

        )}

      </form>

      {
        error && (

          <div className="error-message">
            <AlertCircle className="error-icon" />
            <p>{error}</p>
          </div>

        )
      }

      {
        hasSearched && !loading && !error && data.length === 0 && (

          <div className="no-results">
            <BookOpen size={48} style={{ opacity: 0.5, marginBottom: '1rem' }} />
            <p>No papers found matching your criteria.</p>
          </div>

        )
      }

      {
        data.length > 0 && (

          <div className="results-container">

            <p style={{ color: 'var(--text-secondary)', marginBottom: '0.5rem' }}>
              Page {page} &mdash; Showing {data.length} result{data.length !== 1 ? 's' : ''}
            </p>

            {data.map((paper, index) => (

              <div
                className="paper-card"
                key={index}
              >

                <div className="paper-header">
                  <h2 className="paper-title">{paper.title}</h2>
                </div>

                <div className="paper-authors">
                  <User size={16} />
                  <span>{formatAuthors(paper.authors)}</span>
                </div>

                <p className="paper-abstract">
                  {paper.abstractText
                    ? paper.abstractText
                    : "No abstract available for this paper."}
                </p>

                <div className="paper-footer">

                  <div style={{ display: 'flex', gap: '1rem' }}>

                    <button
                      onClick={() => handleFetchSimilar(paper.id)}
                      className="similar-btn"
                    >
                      <Link size={14} />
                      Find Similar
                    </button>

                    {paper.updateDate && (

                      <div className="paper-date">
                        <Calendar size={16} />
                        {new Date(paper.updateDate).toLocaleDateString()}
                      </div>

                    )}

                  </div>

                  <a
                    href={`https://arxiv.org/pdf/${paper.id}.pdf`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="paper-link"
                  >
                    <ExternalLink size={16} />
                    View PDF
                  </a>

                </div>

              </div>

            ))}

            {/* Pagination controls */}
            <div className="pagination">
              <button
                className="pagination-btn"
                disabled={page === 1 || loading}
                onClick={() => setPage(p => Math.max(1, p - 1))}
              >
                ← Previous
              </button>

              <span className="pagination-info">Page {page} of 10</span>

              <button
                className="pagination-btn"
                disabled={page === 10 || loading || data.length < 10}
                onClick={() => setPage(p => Math.min(10, p + 1))}
              >
                Next →
              </button>
            </div>

          </div>

        )
      }

    </div >


  );

}
