/**
 * FleetPage — topology layout algorithm tests.
 *
 * Verifies that computeLayout() correctly:
 * 1. Assigns x/y to all nodes
 * 2. Assigns columns by type (mcp < worker < task)
 * 3. Vertically distributes nodes within each column
 * 4. Passes edges through unchanged
 * 5. Handles empty graphs
 */
import { describe, it, expect } from 'vitest';
import { computeLayout } from './FleetPage';
import type { TopologyGraph } from '../api/types';

const GRAPH: TopologyGraph = {
  nodes: [
    { id: 'ctr_aaa',       type: 'worker', label: 'worker-alpha', status: 'running' },
    { id: 'ctr_bbb',       type: 'worker', label: 'worker-beta',  status: 'idle'    },
    { id: 'task:ses_111',  type: 'task',   label: 'ses_111',      status: 'active'  },
    { id: 'mcp:mcp_github',type: 'mcp',    label: 'github',       status: 'enabled' },
  ],
  edges: [
    { from: 'ctr_aaa', to: 'task:ses_111',   kind: 'worker→task' },
    { from: 'ctr_aaa', to: 'mcp:mcp_github', kind: 'worker→mcp'  },
    { from: 'ctr_bbb', to: 'mcp:mcp_github', kind: 'worker→mcp'  },
  ],
};

describe('computeLayout', () => {
  it('assigns x and y to every node', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    expect(nodesWithPos).toHaveLength(GRAPH.nodes.length);
    for (const n of nodesWithPos) {
      expect(typeof n.x).toBe('number');
      expect(typeof n.y).toBe('number');
      expect(isNaN(n.x)).toBe(false);
      expect(isNaN(n.y)).toBe(false);
    }
  });

  it('places mcp nodes to the left of worker nodes', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    const mcpX    = nodesWithPos.find(n => n.type === 'mcp')!.x;
    const workerX = nodesWithPos.find(n => n.type === 'worker')!.x;
    expect(mcpX).toBeLessThan(workerX);
  });

  it('places worker nodes to the left of task nodes', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    const workerX = nodesWithPos.find(n => n.type === 'worker')!.x;
    const taskX   = nodesWithPos.find(n => n.type === 'task')!.x;
    expect(workerX).toBeLessThan(taskX);
  });

  it('all columns share the same x within their type', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    const workerXs = nodesWithPos.filter(n => n.type === 'worker').map(n => n.x);
    // All workers share same x value
    expect(new Set(workerXs).size).toBe(1);
  });

  it('distributes multiple same-type nodes at different y values', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    const workerYs = nodesWithPos.filter(n => n.type === 'worker').map(n => n.y);
    // Two workers should have distinct y values
    expect(workerYs[0]).not.toBe(workerYs[1]);
  });

  it('passes edges through unchanged', () => {
    const { edges } = computeLayout(GRAPH);
    expect(edges).toEqual(GRAPH.edges);
  });

  it('single node per column gets y centred at CANVAS_H / 2', () => {
    const singleMcpGraph: TopologyGraph = {
      nodes: [
        { id: 'ctr_x', type: 'worker', label: 'x', status: 'idle' },
        { id: 'mcp:y', type: 'mcp',    label: 'y', status: 'enabled' },
      ],
      edges: [],
    };
    const { nodesWithPos } = computeLayout(singleMcpGraph);
    const mcp    = nodesWithPos.find(n => n.type === 'mcp')!;
    const worker = nodesWithPos.find(n => n.type === 'worker')!;
    expect(mcp.y).toBe(230);    // CANVAS_H (460) / 2
    expect(worker.y).toBe(230);
  });

  it('handles empty graph without throwing', () => {
    const empty: TopologyGraph = { nodes: [], edges: [] };
    const { nodesWithPos, edges } = computeLayout(empty);
    expect(nodesWithPos).toEqual([]);
    expect(edges).toEqual([]);
  });

  it('preserves all node fields in output', () => {
    const { nodesWithPos } = computeLayout(GRAPH);
    const worker = nodesWithPos.find(n => n.id === 'ctr_aaa')!;
    expect(worker.type).toBe('worker');
    expect(worker.label).toBe('worker-alpha');
    expect(worker.status).toBe('running');
  });
});
