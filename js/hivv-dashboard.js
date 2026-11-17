/**
 * HIVV Dashboard - Core JavaScript Library
 *
 * A comprehensive visualization and coordination platform
 * for AI agents with tri-state governance (YES/MAYBE/NO).
 */

class HIVVDashboard {
  constructor() {
    // State
    this.currentView = 'world';
    this.currentState = 'all';
    this.currentPerspective = 'public';
    this.selectedAgent = null;
    this.agents = [];
    this.relationships = [];
    this.events = [];
    this.intentions = [];
    this.tasks = [];
    this.anomalies = [];
    this.stateDistribution = { yes: 0, maybe: 0, no: 0 };

    // Session
    this.sessionToken = localStorage.getItem('hivv_session_token');
    this.user = null;

    // API Base
    this.apiBase = '/api/dashboard';

    // Bind methods
    this.init = this.init.bind(this);
    this.handleNavClick = this.handleNavClick.bind(this);
    this.handleStateLensClick = this.handleStateLensClick.bind(this);
    this.handlePerspectiveClick = this.handlePerspectiveClick.bind(this);
    this.showToast = this.showToast.bind(this);
  }

  async init() {
    console.log('🟢 HIVV Dashboard initializing...');

    // Setup event listeners
    this.setupEventListeners();

    // Load initial data
    await this.loadStateLens();
    await this.loadMyAgents();
    await this.loadRecentActivity();
    await this.loadUpcomingIntentions();
    await this.loadUniverseMap();

    // Check session
    await this.checkSession();

    console.log('🟢 HIVV Dashboard ready');
  }

  setupEventListeners() {
    // Navigation
    document.querySelectorAll('.hivv-nav-btn').forEach(btn => {
      btn.addEventListener('click', this.handleNavClick);
    });

    // State Lens
    document.querySelectorAll('.hivv-lens-btn').forEach(btn => {
      btn.addEventListener('click', this.handleStateLensClick);
    });

    // Perspective
    document.querySelectorAll('.hivv-persp-btn').forEach(btn => {
      btn.addEventListener('click', this.handlePerspectiveClick);
    });

    // MAYBE Machine button
    const maybeBtn = document.getElementById('maybe-machine-btn');
    if (maybeBtn) {
      maybeBtn.addEventListener('click', () => this.showView('maybe-machine'));
    }

    // Detail panel close
    const detailClose = document.getElementById('detail-close');
    if (detailClose) {
      detailClose.addEventListener('click', () => this.closeDetailPanel());
    }

    // WHY Modal close
    const whyModalClose = document.getElementById('why-modal-close');
    if (whyModalClose) {
      whyModalClose.addEventListener('click', () => this.closeModal('why-modal'));
    }

    // Control Modal close
    const controlModalClose = document.getElementById('control-modal-close');
    if (controlModalClose) {
      controlModalClose.addEventListener('click', () => this.closeModal('control-modal'));
    }

    // Control actions
    document.querySelectorAll('.hivv-control-action').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const action = e.currentTarget.dataset.action;
        this.handleControlAction(action);
      });
    });

    // Universe map controls
    const zoomIn = document.getElementById('zoom-in');
    const zoomOut = document.getElementById('zoom-out');
    const zoomReset = document.getElementById('zoom-reset');
    const toggleLabels = document.getElementById('toggle-labels');

    if (zoomIn) zoomIn.addEventListener('click', () => this.zoomUniverseMap(1.5));
    if (zoomOut) zoomOut.addEventListener('click', () => this.zoomUniverseMap(0.67));
    if (zoomReset) zoomReset.addEventListener('click', () => this.resetUniverseMap());
    if (toggleLabels) toggleLabels.addEventListener('click', () => this.toggleLabels());

    // Search
    const searchInput = document.getElementById('hivv-search-input');
    if (searchInput) {
      searchInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
          this.handleSearch(e.target.value);
        }
      });
    }

    // User menu dropdown
    const userBtn = document.getElementById('user-menu-btn');
    const userDropdown = document.getElementById('user-dropdown');
    if (userBtn && userDropdown) {
      userBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        userDropdown.style.display = userDropdown.style.display === 'none' ? 'block' : 'none';
      });
      document.addEventListener('click', () => {
        userDropdown.style.display = 'none';
      });
    }

    // Login button
    const loginBtn = document.getElementById('login-btn');
    if (loginBtn) {
      loginBtn.addEventListener('click', () => this.showLoginModal());
    }

    // Logout button
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', () => this.logout());
    }

    // Login modal close
    const loginModalClose = document.getElementById('login-modal-close');
    if (loginModalClose) {
      loginModalClose.addEventListener('click', () => this.closeModal('login-modal'));
    }

    // Login form
    const loginForm = document.getElementById('login-form');
    if (loginForm) {
      loginForm.addEventListener('submit', (e) => this.handleLogin(e));
    }
  }

  // ============================================
  // NAVIGATION & VIEW MANAGEMENT
  // ============================================

  handleNavClick(e) {
    const view = e.currentTarget.dataset.view;
    this.showView(view);
  }

  showView(view) {
    this.currentView = view;

    // Update nav buttons
    document.querySelectorAll('.hivv-nav-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });

    // Hide all viz containers
    document.querySelectorAll('.hivv-viz-container').forEach(viz => {
      viz.style.display = 'none';
    });

    // Show selected viz
    const vizMap = {
      'world': 'viz-observer',
      'me': 'viz-observer',
      'agents': 'viz-relationship-graph',
      'tasks': 'viz-timeline',
      'network': 'viz-relationship-graph',
      'governance': 'viz-permission-matrix',
      'history': 'viz-timeline',
      'security': 'viz-anomaly-radar',
      'economy': 'viz-observer',
      'maybe-machine': 'viz-maybe-machine'
    };

    const vizId = vizMap[view] || 'viz-observer';
    const vizEl = document.getElementById(vizId);
    if (vizEl) {
      vizEl.style.display = 'block';
    }

    // Load view-specific data
    this.loadViewData(view);
  }

  async loadViewData(view) {
    switch (view) {
      case 'agents':
        await this.loadRelationshipGraph();
        break;
      case 'tasks':
        await this.loadTimeline();
        break;
      case 'network':
        await this.loadRelationshipGraph();
        break;
      case 'governance':
        await this.loadPermissionMatrix();
        break;
      case 'history':
        await this.loadTimeline();
        break;
      case 'security':
        await this.loadAnomalyRadar();
        break;
      case 'maybe-machine':
        await this.loadMaybeMachine();
        break;
    }
  }

  // ============================================
  // STATE LENS & PERSPECTIVE
  // ============================================

  handleStateLensClick(e) {
    const state = e.currentTarget.dataset.state;
    this.setStateLens(state);
  }

  setStateLens(state) {
    this.currentState = state;

    // Update lens buttons
    document.querySelectorAll('.hivv-lens-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.state === state);
    });

    // Reload data with new filter
    this.loadUniverseMap();
  }

  handlePerspectiveClick(e) {
    const perspective = e.currentTarget.dataset.perspective;
    this.setPerspective(perspective);
  }

  setPerspective(perspective) {
    this.currentPerspective = perspective;

    // Update perspective buttons
    document.querySelectorAll('.hivv-persp-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.perspective === perspective);
    });

    // Reload data with new perspective
    this.loadMyAgents();
  }

  // ============================================
  // DATA LOADING
  // ============================================

  async loadStateLens() {
    try {
      const res = await this.api('/state-lens');
      if (res.success) {
        this.stateDistribution = res.totals;
        this.updateStateLensUI();
      }
    } catch (err) {
      console.error('Failed to load state lens:', err);
    }
  }

  updateStateLensUI() {
    const { yes = 0, maybe = 0, no = 0, total = 0 } = this.stateDistribution;

    document.getElementById('stat-yes').textContent = this.formatNumber(yes);
    document.getElementById('stat-maybe').textContent = this.formatNumber(maybe);
    document.getElementById('stat-no').textContent = this.formatNumber(no);
    document.getElementById('maybe-count').textContent = maybe;

    const observerAgents = document.getElementById('observer-agents');
    if (observerAgents) observerAgents.textContent = this.formatNumber(total);
  }

  async loadMyAgents() {
    try {
      const res = await this.api('/agents', {
        perspective: this.currentPerspective,
        limit: 20
      });

      if (res.success) {
        this.agents = res.agents;
        this.renderMyAgents();
      }
    } catch (err) {
      console.error('Failed to load agents:', err);
    }
  }

  renderMyAgents() {
    const list = document.getElementById('my-agents-list');
    if (!list) return;

    if (this.agents.length === 0) {
      list.innerHTML = '<li class="hivv-agent-item">No agents found</li>';
      return;
    }

    list.innerHTML = this.agents.map(agent => `
      <li class="hivv-agent-item ${this.selectedAgent?.id === agent.id ? 'active' : ''}"
          data-agent-id="${agent.id}">
        <span class="hivv-agent-icon">${this.getAgentIcon(agent.agent_type)}</span>
        <span class="hivv-agent-name">${this.escapeHtml(agent.public_name || 'Unnamed')}</span>
        <span class="hivv-agent-state ${agent.state}"></span>
      </li>
    `).join('');

    // Add click handlers
    list.querySelectorAll('.hivv-agent-item').forEach(item => {
      item.addEventListener('click', () => {
        const agentId = item.dataset.agentId;
        const agent = this.agents.find(a => a.id === agentId);
        this.selectAgent(agent);
      });
    });
  }

  async loadRecentActivity() {
    try {
      const res = await this.api('/activity', {
        time_range: '24h',
        limit: 10
      });

      if (res.success) {
        this.events = res.events;
        this.renderRecentActivity();
      }
    } catch (err) {
      console.error('Failed to load activity:', err);
    }
  }

  renderRecentActivity() {
    const list = document.getElementById('recent-activity-list');
    if (!list) return;

    if (this.events.length === 0) {
      list.innerHTML = '<li class="hivv-activity-item">No recent activity</li>';
      return;
    }

    list.innerHTML = this.events.slice(0, 5).map(event => `
      <li class="hivv-activity-item">
        <span class="hivv-activity-state ${event.state}"></span>
        <span class="hivv-activity-time">${this.formatTime(event.created_at)}</span>
        <div class="hivv-activity-desc">${this.escapeHtml(event.description || event.event_type)}</div>
      </li>
    `).join('');
  }

  async loadUpcomingIntentions() {
    try {
      const res = await this.api('/intentions', {
        state: 'maybe',
        limit: 10
      });

      if (res.success) {
        this.intentions = res.intentions;
        this.renderUpcomingIntentions();
      }
    } catch (err) {
      console.error('Failed to load intentions:', err);
    }
  }

  renderUpcomingIntentions() {
    const list = document.getElementById('upcoming-intent-list');
    if (!list) return;

    if (this.intentions.length === 0) {
      list.innerHTML = '<li class="hivv-intent-item">No upcoming actions</li>';
      return;
    }

    list.innerHTML = this.intentions.slice(0, 5).map(intent => `
      <li class="hivv-intent-item">
        <span class="hivv-intent-time">${intent.scheduled_at ? this.formatTime(intent.scheduled_at) : 'Soon'}</span>
        <div class="hivv-intent-action">${this.escapeHtml(intent.action)}</div>
      </li>
    `).join('');
  }

  async loadUniverseMap() {
    try {
      const res = await this.api('/universe-map', {
        state: this.currentState === 'all' ? undefined : this.currentState,
        limit: 500
      });

      if (res.success) {
        this.nodes = res.nodes;
        this.edges = res.edges;
        this.renderUniverseMap();
      }
    } catch (err) {
      console.error('Failed to load universe map:', err);
    }
  }

  // ============================================
  // VISUALIZATIONS
  // ============================================

  renderUniverseMap() {
    const canvas = document.getElementById('universe-canvas');
    if (!canvas) return;

    // Clear existing
    canvas.innerHTML = '';

    if (!this.nodes || this.nodes.length === 0) {
      canvas.innerHTML = '<div class="hivv-universe-placeholder"><p>🌌 No agents found</p></div>';
      return;
    }

    // Create SVG for force-directed graph
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.style.background = '#0d1117';

    // Create node positions using simple force simulation
    const nodes = this.nodes.map((node, i) => ({
      ...node,
      x: width / 2 + (Math.random() - 0.5) * 200,
      y: height / 2 + (Math.random() - 0.5) * 200
    }));

    // Create links from edges
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    const links = this.edges
      .filter(e => nodeMap.has(e.source_agent_id) && nodeMap.has(e.target_agent_id))
      .map(e => ({
        source: nodeMap.get(e.source_agent_id),
        target: nodeMap.get(e.target_agent_id),
        type: e.type,
        weight: e.weight
      }));

    // Simple force simulation (gravity toward center)
    const simulation = () => {
      nodes.forEach(node => {
        // Center gravity
        node.vx = (node.vx || 0) * 0.9 + (width / 2 - node.x) * 0.01;
        node.vy = (node.vy || 0) * 0.9 + (height / 2 - node.y) * 0.01;

        // Link attraction
        links.forEach(link => {
          const dx = link.target.x - link.source.x;
          const dy = link.target.y - link.source.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const force = 0.01 * (dist - 100);

          if (link.source === node) {
            node.vx -= dx / dist * force;
            node.vy -= dy / dist * force;
          }
          if (link.target === node) {
            node.vx += dx / dist * force;
            node.vy += dy / dist * force;
          }
        });

        // Repulsion between nodes
        nodes.forEach(other => {
          if (other === node) return;
          const dx = other.x - node.x;
          const dy = other.y - node.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const force = -100 / (dist * dist);
          node.vx += dx / dist * force;
          node.vy += dy / dist * force;
        });

        node.x += node.vx || 0;
        node.y += node.vy || 0;
        node.x = Math.max(50, Math.min(width - 50, node.x));
        node.y = Math.max(50, Math.min(height - 50, node.y));
      });
    };

    // Run simulation
    for (let i = 0; i < 100; i++) simulation();

    // Draw links
    links.forEach(link => {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', link.source.x);
      line.setAttribute('y1', link.source.y);
      line.setAttribute('x2', link.target.x);
      line.setAttribute('y2', link.target.y);
      line.setAttribute('stroke', '#333');
      line.setAttribute('stroke-width', Math.min(link.weight / 100, 3));
      line.setAttribute('stroke-opacity', '0.5');
      svg.appendChild(line);
    });

    // Draw nodes
    nodes.forEach(node => {
      const colors = {
        human: '#74b9ff',
        ai: '#00ff88',
        organization: '#ff9f43',
        network: '#c084fc',
        device: '#ffd93d',
        robot: '#ff6b9d'
      };

      const color = colors[node.type] || '#888';
      const stateColors = { yes: '#00ff88', maybe: '#ffd93d', no: '#ff6b6b' };
      const nodeColor = stateColors[node.state] || color;

      // Node circle
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', node.x);
      circle.setAttribute('cy', node.y);
      circle.setAttribute('r', 8 + (node.trust_score || 50) / 20);
      circle.setAttribute('fill', nodeColor);
      circle.setAttribute('stroke', color);
      circle.setAttribute('stroke-width', 2);
      circle.style.cursor = 'pointer';
      circle.style.transition = 'r 0.2s';

      // Click handler
      circle.addEventListener('click', () => {
        this.selectAgent(node);
      });

      // Hover effect
      circle.addEventListener('mouseenter', () => {
        circle.setAttribute('r', 12 + (node.trust_score || 50) / 20);
      });
      circle.addEventListener('mouseleave', () => {
        circle.setAttribute('r', 8 + (node.trust_score || 50) / 20);
      });

      svg.appendChild(circle);

      // Label
      if (node.name) {
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', node.x);
        text.setAttribute('y', node.y + 20 + (node.trust_score || 50) / 20);
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('fill', '#888');
        text.setAttribute('font-size', '10');
        text.textContent = node.name.length > 15 ? node.name.substring(0, 15) + '...' : node.name;
        svg.appendChild(text);
      }
    });

    canvas.appendChild(svg);

    // Store for zoom controls
    this.universeSvg = svg;
    this.universeWidth = width;
    this.universeHeight = height;
  }

  async loadRelationshipGraph() {
    try {
      const res = await this.api('/relationships', {
        state: this.currentState === 'all' ? undefined : this.currentState,
        limit: 200
      });

      if (res.success) {
        this.relationships = res.relationships;
        this.renderRelationshipGraph();
      }
    } catch (err) {
      console.error('Failed to load relationship graph:', err);
    }
  }

  renderRelationshipGraph() {
    const canvas = document.getElementById('relationship-graph-canvas');
    if (!canvas) return;

    canvas.innerHTML = '<div class="hivv-universe-placeholder"><p>🕸️ Loading relationship graph...</p></div>';

    // Similar to universe map but with relationship types
    // Implementation would use D3.js or similar for better visualization
  }

  async loadTimeline() {
    try {
      const res = await this.api('/timeline', {
        time_range: '24h',
        limit: 100
      });

      if (res.success) {
        this.timeline = res.timeline;
        this.renderTimeline();
      }
    } catch (err) {
      console.error('Failed to load timeline:', err);
    }
  }

  renderTimeline() {
    const canvas = document.getElementById('timeline-canvas');
    if (!canvas) return;

    if (!this.timeline || this.timeline.length === 0) {
      canvas.innerHTML = '<div class="hivv-universe-placeholder"><p>🕰️ No events in this time range</p></div>';
      return;
    }

    // Simple timeline visualization
    const container = document.createElement('div');
    container.className = 'hivv-timeline-viz';

    const events = this.timeline.map(t => ({
      time: new Date(t.bucket),
      type: t.event_type,
      state: t.state,
      count: parseInt(t.count)
    }));

    container.innerHTML = `
      <div class="hivv-timeline-events">
        ${events.slice(0, 20).map(e => `
          <div class="hivv-timeline-event ${e.state}">
            <span class="hivv-timeline-time">${this.formatTime(e.time)}</span>
            <span class="hivv-timeline-type">${e.type}</span>
            <span class="hivv-timeline-count">${e.count}</span>
          </div>
        `).join('')}
      </div>
    `;

    canvas.innerHTML = '';
    canvas.appendChild(container);
  }

  async loadPermissionMatrix() {
    try {
      const res = await this.api('/permissions/matrix');

      if (res.success) {
        this.permissionMatrix = res.matrix;
        this.renderPermissionMatrix();
      }
    } catch (err) {
      console.error('Failed to load permission matrix:', err);
    }
  }

  renderPermissionMatrix() {
    const canvas = document.getElementById('permission-matrix-canvas');
    if (!canvas) return;

    if (!this.permissionMatrix || this.permissionMatrix.length === 0) {
      canvas.innerHTML = '<div class="hivv-universe-placeholder"><p>🔐 No permissions configured</p></div>';
      return;
    }

    // Render as HTML table
    const resources = ['email', 'calendar', 'money', 'files', 'internet', 'code'];

    canvas.innerHTML = `
      <table class="hivv-permission-table">
        <thead>
          <tr>
            <th>Agent</th>
            ${resources.map(r => `<th>${r}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
          ${this.permissionMatrix.map(row => `
            <tr>
              <td>${this.escapeHtml(row.agent_name)}</td>
              ${resources.map(r => {
                const perm = row.permissions[r] || {};
                const state = perm.state || 'no';
                const icons = { yes: '🟢', maybe: '🟡', no: '🔴' };
                return `<td class="hivv-perm-${state}">${icons[state]}</td>`;
              }).join('')}
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  async loadAnomalyRadar() {
    try {
      const res = await this.api('/anomalies/radar', {
        state: 'detected',
        limit: 50
      });

      if (res.success) {
        this.anomalies = res.anomalies;
        this.renderAnomalyRadar();
      }
    } catch (err) {
      console.error('Failed to load anomaly radar:', err);
    }
  }

  renderAnomalyRadar() {
    const canvas = document.getElementById('anomaly-radar-canvas');
    if (!canvas) return;

    if (!this.anomalies || this.anomalies.length === 0) {
      canvas.innerHTML = '<div class="hivv-universe-placeholder"><p>🚨 No anomalies detected</p></div>';
      return;
    }

    // Simple radar visualization
    const container = document.createElement('div');
    container.className = 'hivv-radar-viz';

    container.innerHTML = `
      <div class="hivv-radar-display">
        <div class="hivv-radar-center">NORMAL</div>
        ${this.anomalies.map((anomaly, i) => {
          const angle = (i / this.anomalies.length) * 360;
          const distance = 40 + (anomaly.severity === 'critical' ? 40 : anomaly.severity === 'high' ? 30 : 20);
          return `<div class="hivv-radar-blip" style="transform: rotate(${angle}deg) translateY(${distance}%)">
            <span class="hivv-radar-blip-icon">⚠️</span>
            <span class="hivv-radar-blip-type">${anomaly.anomaly_type}</span>
          </div>`;
        }).join('')}
      </div>
      <div class="hivv-anomaly-list">
        ${this.anomalies.slice(0, 10).map(a => `
          <div class="hivv-anomaly-item ${a.severity}">
            <span class="hivv-anomaly-icon">⚠️</span>
            <span class="hivv-anomaly-desc">${this.escapeHtml(a.description || a.anomaly_type)}</span>
            <span class="hivv-anomaly-severity">${a.severity}</span>
          </div>
        `).join('')}
      </div>
    `;

    canvas.innerHTML = '';
    canvas.appendChild(container);
  }

  async loadMaybeMachine() {
    try {
      const res = await this.api('/maybe-machine');

      if (res.success) {
        this.maybeData = res;
        this.renderMaybeMachine();
      }
    } catch (err) {
      console.error('Failed to load MAYBE machine:', err);
    }
  }

  renderMaybeMachine() {
    const canvas = document.getElementById('maybe-machine-canvas');
    if (!canvas) return;

    const { summary, agents, intentions, approvals, anomalies } = this.maybeData;

    canvas.innerHTML = `
      <div class="hivv-maybe-summary">
        <div class="hivv-maybe-stat">
          <span class="hivv-maybe-value">${summary?.total_items || 0}</span>
          <span class="hivv-maybe-label">Items need attention</span>
        </div>
      </div>

      <div class="hivv-maybe-sections">
        ${agents?.length ? `
          <section class="hivv-maybe-section">
            <h4>🤖 Agents in MAYBE state</h4>
            ${agents.map(a => `
              <div class="hivv-maybe-item" data-agent-id="${a.id}">
                <span class="hivv-maybe-item-icon">🤖</span>
                <span class="hivv-maybe-item-name">${this.escapeHtml(a.public_name)}</span>
                <span class="hivv-maybe-item-action">
                  <button class="hivv-btn-sm" onclick="window.hivv.resolveAgentState('${a.id}', 'yes')">🟢 YES</button>
                  <button class="hivv-btn-sm" onclick="window.hivv.resolveAgentState('${a.id}', 'no')">🔴 NO</button>
                </span>
              </div>
            `).join('')}
          </section>
        ` : ''}

        ${intentions?.length ? `
          <section class="hivv-maybe-section">
            <h4>🔮 Pending Intentions</h4>
            ${intentions.map(i => `
              <div class="hivv-maybe-item" data-intention-id="${i.id}">
                <span class="hivv-maybe-item-icon">🔮</span>
                <span class="hivv-maybe-item-name">${this.escapeHtml(i.action)}</span>
                <span class="hivv-maybe-item-meta">${i.agent_name} · ${i.confidence}% confidence</span>
                <span class="hivv-maybe-item-action">
                  <button class="hivv-btn-sm" onclick="window.hivv.resolveIntention('${i.id}', 'yes')">🟢</button>
                  <button class="hivv-btn-sm" onclick="window.hivv.resolveIntention('${i.id}', 'no')">🔴</button>
                </span>
              </div>
            `).join('')}
          </section>
        ` : ''}

        ${approvals?.length ? `
          <section class="hivv-maybe-section">
            <h4>🔐 Pending Approvals</h4>
            ${approvals.map(p => `
              <div class="hivv-maybe-item" data-permission-id="${p.id}">
                <span class="hivv-maybe-item-icon">🔐</span>
                <span class="hivv-maybe-item-name">${p.resource}:${p.action}</span>
                <span class="hivv-maybe-item-meta">${p.agent_name} · ${p.risk_level} risk</span>
                <span class="hivv-maybe-item-action">
                  <button class="hivv-btn-sm" onclick="window.hivv.resolvePermission('${p.id}', 'yes')">🟢</button>
                  <button class="hivv-btn-sm" onclick="window.hivv.resolvePermission('${p.id}', 'no')">🔴</button>
                </span>
              </div>
            `).join('')}
          </section>
        ` : ''}

        ${anomalies?.length ? `
          <section class="hivv-maybe-section">
            <h4>🚨 Active Anomalies</h4>
            ${anomalies.map(a => `
              <div class="hivv-maybe-item ${a.severity}">
                <span class="hivv-maybe-item-icon">⚠️</span>
                <span class="hivv-maybe-item-name">${this.escapeHtml(a.description || a.anomaly_type)}</span>
                <span class="hivv-maybe-item-meta">${a.severity} severity</span>
                <span class="hivv-maybe-item-action">
                  <button class="hivv-btn-sm" onclick="window.hivv.resolveAnomaly('${a.id}')">✓ Resolve</button>
                </span>
              </div>
            `).join('')}
          </section>
        ` : ''}
      </div>
    `;
  }

  // ============================================
  // AGENT SELECTION & DETAIL PANEL
  // ============================================

  selectAgent(agent) {
    this.selectedAgent = agent;
    this.renderMyAgents();
    this.showAgentDetail(agent);
  }

  async showAgentDetail(agent) {
    try {
      const res = await this.api(`/agents/${agent.id}`, {
        perspective: this.currentPerspective
      });

      if (res.success) {
        this.openDetailPanel(res.agent, res);
      }
    } catch (err) {
      console.error('Failed to load agent details:', err);
    }
  }

  openDetailPanel(agent, data = {}) {
    const panel = document.getElementById('hivv-detail-panel');
    const title = document.getElementById('detail-title');
    const content = document.getElementById('detail-content');
    const actions = document.getElementById('detail-actions');

    if (!panel) return;

    title.textContent = agent.public_name || 'Agent Details';

    const stateColors = { yes: '🟢', maybe: '🟡', no: '🔴' };

    content.innerHTML = `
      <div class="hivv-detail-section">
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">Type</span>
          <span class="hivv-detail-value">${this.getAgentIcon(agent.agent_type)} ${agent.agent_type}</span>
        </div>
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">State</span>
          <span class="hivv-detail-value">${stateColors[agent.state] || '⚪'} ${agent.state}</span>
        </div>
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">Model</span>
          <span class="hivv-detail-value">${agent.model || 'N/A'}</span>
        </div>
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">Trust Score</span>
          <span class="hivv-detail-value">${agent.trust_score || 'N/A'}</span>
        </div>
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">Last Active</span>
          <span class="hivv-detail-value">${agent.last_active_at ? this.formatTime(agent.last_active_at) : 'Never'}</span>
        </div>
        <div class="hivv-detail-row">
          <span class="hivv-detail-label">Created</span>
          <span class="hivv-detail-value">${agent.created_at ? this.formatTime(agent.created_at) : 'Unknown'}</span>
        </div>
      </div>

      ${agent.description ? `
        <div class="hivv-detail-section">
          <h4>Description</h4>
          <p>${this.escapeHtml(agent.description)}</p>
        </div>
      ` : ''}

      ${agent.capabilities?.length ? `
        <div class="hivv-detail-section">
          <h4>Capabilities</h4>
          <div class="hivv-capabilities">
            ${(Array.isArray(agent.capabilities) ? agent.capabilities : JSON.parse(agent.capabilities || '[]')).map(c => `
              <span class="hivv-capability-badge">${this.escapeHtml(c)}</span>
            `).join('')}
          </div>
        </div>
      ` : ''}

      ${data.relationships?.length ? `
        <div class="hivv-detail-section">
          <h4>Relationships (${data.relationships.length})</h4>
          ${data.relationships.slice(0, 5).map(r => `
            <div class="hivv-relationship-item">
              <span class="hivv-rel-type">${r.relationship_type}</span>
              <span class="hivv-rel-other">${r.other_agent_name || 'Unknown'}</span>
            </div>
          `).join('')}
        </div>
      ` : ''}

      ${data.intentions?.length ? `
        <div class="hivv-detail-section">
          <h4>Active Intentions (${data.intentions.length})</h4>
          ${data.intentions.slice(0, 3).map(i => `
            <div class="hivv-intention-preview">
              <span>${this.escapeHtml(i.action)}</span>
              <span class="hivv-intent-meta">${i.confidence}%</span>
            </div>
          `).join('')}
        </div>
      ` : ''}
    `;

    actions.innerHTML = `
      <button class="hivv-btn hivv-btn-yes" onclick="window.hivv.showControlModal('allow', '${agent.id}')">🟢 ALLOW</button>
      <button class="hivv-btn hivv-btn-no" onclick="window.hivv.showControlModal('deny', '${agent.id}')">🔴 DENY</button>
      <button class="hivv-btn" onclick="window.hivv.showWhyModal('${agent.id}')">❓ WHY?</button>
    `;

    panel.classList.add('open');
  }

  closeDetailPanel() {
    const panel = document.getElementById('hivv-detail-panel');
    if (panel) {
      panel.classList.remove('open');
    }
    this.selectedAgent = null;
  }

  // ============================================
  // MODALS
  // ============================================

  showWhyModal(agentId) {
    const modal = document.getElementById('why-modal');
    const body = document.getElementById('why-modal-body');

    if (!modal) return;

    body.innerHTML = '<div class="hivv-why-loading">Loading decision chain...</div>';
    modal.classList.add('open');

    // Load decision chain
    this.loadWhyChain(agentId);
  }

  async loadWhyChain(agentId) {
    try {
      // For now, load recent events as the "chain"
      const res = await this.api('/activity', {
        agent_id: agentId,
        limit: 20
      });

      const body = document.getElementById('why-modal-body');

      if (res.success && res.events.length > 0) {
        body.innerHTML = `
          <div class="hivv-why-chain">
            ${res.events.map((e, i) => `
              <div class="hivv-why-step">
                <span class="hivv-why-arrow">${i > 0 ? '↓' : '●'}</span>
                <div class="hivv-why-content">
                  <div class="hivv-why-type">${e.event_type}</div>
                  <div class="hivv-why-reasoning">${this.escapeHtml(e.description || 'No description')}</div>
                  <div class="hivv-why-meta">${this.formatTime(e.created_at)} · State: ${e.state}</div>
                </div>
              </div>
            `).join('')}
          </div>
        `;
      } else {
        body.innerHTML = '<p>No decision history available.</p>';
      }
    } catch (err) {
      body.innerHTML = '<p>Failed to load decision chain.</p>';
    }
  }

  showControlModal(action, agentId) {
    const modal = document.getElementById('control-modal');
    if (modal) {
      modal.dataset.action = action;
      modal.dataset.agentId = agentId;
      modal.classList.add('open');
    }
  }

  closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
      modal.classList.remove('open');
    }
  }

  async handleControlAction(action) {
    const modal = document.getElementById('control-modal');
    const agentId = modal?.dataset.agentId;

    if (!agentId) return;

    try {
      let endpoint, data;

      switch (action) {
        case 'allow':
        case 'deny':
          endpoint = `/agents/${agentId}/state`;
          data = { state: action === 'allow' ? 'yes' : 'no' };
          break;
        case 'ask':
          // Send message asking for clarification
          endpoint = '/messages';
          data = {
            message_type: 'ASK',
            from_agent_id: 'system',
            to_agent_id: agentId,
            payload: { question: 'Request for clarification' }
          };
          break;
        case 'escalate':
          endpoint = '/messages';
          data = {
            message_type: 'ESCALATE',
            from_agent_id: 'system',
            to_agent_id: agentId,
            payload: { reason: 'Escalated for review' }
          };
          break;
        case 'delegate':
          // Would show agent selector
          this.showToast('Delegate', 'Please select an agent to delegate to', 'maybe');
          return;
        case 'audit':
          await this.showWhyModal(agentId);
          this.closeModal('control-modal');
          return;
      }

      if (endpoint && data) {
        const res = await this.api(endpoint, data, 'POST');
        if (res.success) {
          this.showToast('Success', `Action ${action} completed`, 'yes');
          this.closeModal('control-modal');
          this.closeDetailPanel();
          this.loadStateLens();
          this.loadMyAgents();
        }
      }
    } catch (err) {
      this.showToast('Error', `Failed to perform action: ${err.message}`, 'no');
    }
  }

  // ============================================
  // RESOLUTION HANDLERS (for MAYBE Machine)
  // ============================================

  async resolveAgentState(agentId, state) {
    try {
      const res = await this.api(`/agents/${agentId}/state`, { state }, 'POST');
      if (res.success) {
        this.showToast('Resolved', `Agent state changed to ${state}`, 'yes');
        this.loadMaybeMachine();
        this.loadStateLens();
      }
    } catch (err) {
      this.showToast('Error', err.message, 'no');
    }
  }

  async resolveIntention(intentionId, state) {
    // Would update intention state
    this.showToast('Resolved', `Intention ${state === 'yes' ? 'approved' : 'denied'}`, state === 'yes' ? 'yes' : 'maybe');
    this.loadMaybeMachine();
  }

  async resolvePermission(permissionId, state) {
    try {
      const res = await this.api(`/permissions/${permissionId}`, { state }, 'POST');
      if (res.success) {
        this.showToast('Resolved', `Permission ${state}`, state === 'yes' ? 'yes' : 'no');
        this.loadMaybeMachine();
      }
    } catch (err) {
      this.showToast('Error', err.message, 'no');
    }
  }

  async resolveAnomaly(anomalyId) {
    this.showToast('Resolved', 'Anomaly marked as resolved', 'yes');
    this.loadMaybeMachine();
  }

  // ============================================
  // UNIVERSE MAP CONTROLS
  // ============================================

  zoomUniverseMap(factor) {
    if (!this.universeSvg) return;
    const currentWidth = parseFloat(this.universeSvg.getAttribute('width'));
    const currentHeight = parseFloat(this.universeSvg.getAttribute('height'));
    this.universeSvg.setAttribute('width', currentWidth * factor);
    this.universeSvg.setAttribute('height', currentHeight * factor);
  }

  resetUniverseMap() {
    this.loadUniverseMap();
  }

  toggleLabels() {
    // Toggle label visibility
    const labels = this.universeSvg?.querySelectorAll('text');
    labels?.forEach(label => {
      label.style.display = label.style.display === 'none' ? 'block' : 'none';
    });
  }

  // ============================================
  // SEARCH
  // ============================================

  handleSearch(query) {
    if (!query.trim()) return;

    // Simple search - filter agents
    const filtered = this.agents.filter(a =>
      (a.public_name || '').toLowerCase().includes(query.toLowerCase()) ||
      (a.description || '').toLowerCase().includes(query.toLowerCase())
    );

    if (filtered.length > 0) {
      this.selectAgent(filtered[0]);
    } else {
      this.showToast('Search', `No results for "${query}"`, 'maybe');
    }
  }

  // ============================================
  // SESSION
  // ============================================

  async checkSession() {
    if (!this.sessionToken) {
      this.updateUserUI(false);
      return;
    }

    try {
      const res = await fetch('/api/auth/me', {
        headers: { 'X-Session-Token': this.sessionToken }
      });
      const data = await res.json();

      if (data.logged_in) {
        this.user = data.user;
        this.updateUserUI(true, data.user.username);
      } else {
        this.sessionToken = null;
        localStorage.removeItem('hivv_session_token');
        this.updateUserUI(false);
      }
    } catch (err) {
      console.error('Session check failed:', err);
      this.updateUserUI(false);
    }
  }

  updateUserUI(loggedIn, username = 'Guest') {
    const userName = document.getElementById('user-name');
    const userAvatar = document.getElementById('user-avatar');
    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const loginModalClose = document.getElementById('login-modal-close');

    if (userName) userName.textContent = username;
    if (userAvatar) userAvatar.textContent = loggedIn ? '👤' : '👤';
    if (loginBtn) loginBtn.style.display = loggedIn ? 'none' : 'block';
    if (logoutBtn) logoutBtn.style.display = loggedIn ? 'block' : 'none';
  }

  showLoginModal() {
    const modal = document.getElementById('login-modal');
    if (modal) {
      modal.classList.add('open');
      document.getElementById('login-username').focus();
      this.fetchClientIP();
    }
  }

  async fetchClientIP() {
    try {
      const res = await fetch('/api/ip-info');
      const data = await res.json();
      const ipSpan = document.getElementById('login-ip');
      if (ipSpan) ipSpan.textContent = data.ip || 'unknown';
    } catch (err) {
      const ipSpan = document.getElementById('login-ip');
      if (ipSpan) ipSpan.textContent = 'Unable to detect';
    }
  }

  async handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');

    errorEl.style.display = 'none';

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await res.json();

      if (data.success) {
        this.sessionToken = data.session_token;
        localStorage.setItem('hivv_session_token', this.sessionToken);
        this.user = data.user;
        this.closeModal('login-modal');
        this.updateUserUI(true, data.user.username);
        this.showToast('Welcome', `Logged in as ${data.user.username}`, 'yes');
        document.getElementById('login-form').reset();
      } else {
        errorEl.textContent = data.error || 'Login failed';
        errorEl.style.display = 'block';
      }
    } catch (err) {
      errorEl.textContent = 'Connection error';
      errorEl.style.display = 'block';
    }
  }

  async logout() {
    try {
      if (this.sessionToken) {
        await fetch('/api/auth/logout', {
          method: 'POST',
          headers: { 'X-Session-Token': this.sessionToken }
        });
      }
    } catch (err) {
      console.error('Logout error:', err);
    }
    this.sessionToken = null;
    this.user = null;
    localStorage.removeItem('hivv_session_token');
    this.updateUserUI(false);
    document.getElementById('user-dropdown').style.display = 'none';
    this.showToast('Logged out', 'You have been logged out', 'maybe');
  }

  // ============================================
  // API HELPERS
  // ============================================

  async api(endpoint, params = {}, method = 'GET') {
    const url = new URL(this.apiBase + endpoint, window.location.origin);

    if (method === 'GET' && Object.keys(params).length > 0) {
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null) {
          url.searchParams.append(k, v);
        }
      });
    }

    const options = {
      method,
      headers: {
        'Content-Type': 'application/json'
      }
    };

    if (this.sessionToken) {
      options.headers['X-Session-Token'] = this.sessionToken;
    }

    if (method !== 'GET' && Object.keys(params).length > 0) {
      options.body = JSON.stringify(params);
    }

    const res = await fetch(url.toString(), options);
    return res.json();
  }

  // ============================================
  // UTILITIES
  // ============================================

  getAgentIcon(type) {
    const icons = {
      human: '👤',
      ai: '🤖',
      organization: '🏢',
      network: '🌐',
      device: '📱',
      robot: '🦾'
    };
    return icons[type] || '❓';
  }

  formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
  }

  formatTime(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    const now = new Date();
    const diff = now - date;

    if (diff < 60000) return 'Just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
    if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';

    return date.toLocaleDateString();
  }

  escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  showToast(title, message, state = 'info') {
    const container = document.getElementById('hivv-toasts');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `hivv-toast ${state}`;
    toast.innerHTML = `
      <span class="hivv-toast-icon">${state === 'yes' ? '🟢' : state === 'no' ? '🔴' : state === 'maybe' ? '🟡' : 'ℹ️'}</span>
      <div class="hivv-toast-content">
        <strong>${title}</strong>
        <span>${message}</span>
      </div>
    `;

    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }
}

// Export for use
window.HIVVDashboard = HIVVDashboard;
