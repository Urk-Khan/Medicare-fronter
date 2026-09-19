import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { getToken, isSuperAdmin, onUnauthorized } from './api';
import Layout from './components/Layout';
import { ToastProvider } from './components/ui';
import Agents from './pages/Agents';
import AuditLog from './pages/AuditLog';
import CallDetails from './pages/CallDetails';
import CallHistory from './pages/CallHistory';
import Closers from './pages/Closers';
import Dashboard from './pages/Dashboard';
import DncList from './pages/DncList';
import Followups from './pages/Followups';
import ImportLeads from './pages/ImportLeads';
import Leads from './pages/Leads';
import Login from './pages/Login';
import Settings from './pages/Settings';
import Users from './pages/Users';
import './styles.css';

function Protected({ children, superOnly }) {
  const navigate = useNavigate();
  const [authed, setAuthed] = useState(Boolean(getToken()));
  useEffect(() => onUnauthorized(() => { setAuthed(false); navigate('/login'); }), [navigate]);
  if (!authed) return <Navigate to="/login" replace />;
  if (superOnly && !isSuperAdmin()) return <Navigate to="/" replace />;
  return <Layout>{children}</Layout>;
}

function App() {
  const page = (el) => <Protected>{el}</Protected>;
  const superPage = (el) => <Protected superOnly>{el}</Protected>;
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={page(<Dashboard />)} />
      <Route path="/calls" element={page(<CallHistory />)} />
      <Route path="/calls/:id" element={page(<CallDetails />)} />
      <Route path="/followups" element={page(<Followups />)} />
      <Route path="/leads" element={page(<Leads />)} />
      <Route path="/closers" element={page(<Closers />)} />
      <Route path="/dnc" element={page(<DncList />)} />
      <Route path="/import" element={page(<ImportLeads />)} />
      <Route path="/agents" element={superPage(<Agents />)} />
      <Route path="/users" element={superPage(<Users />)} />
      <Route path="/audit" element={superPage(<AuditLog />)} />
      <Route path="/settings" element={page(<Settings />)} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <App />
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
);
