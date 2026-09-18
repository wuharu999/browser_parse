import { describe, expect, it } from 'vitest';
import { parseRoute } from '../src/grill/router';
import { isAllowedGrillExtension } from '../src/grill/grill_intake';
import { generateGrillMarkdown } from '../src/grill/grill_report';
import { GrillReport, GrillSession, GrillQuestion, CustomerAnswer } from '../src/grill/types';

describe('Grill Bot routing', () => {
  it('identifies log mode routes', () => {
    expect(parseRoute('/')).toEqual({ mode: 'log' });
    expect(parseRoute('/log')).toEqual({ mode: 'log' });
    expect(parseRoute('/unknown')).toEqual({ mode: 'log' });
  });

  it('identifies grill intake route', () => {
    expect(parseRoute('/grill')).toEqual({ mode: 'grill' });
    expect(parseRoute('/grill/')).toEqual({ mode: 'grill' });
  });

  it('identifies private grill session token routes', () => {
    expect(parseRoute('/grill/s/sec_tok_12345')).toEqual({
      mode: 'grill',
      token: 'sec_tok_12345',
    });
    expect(parseRoute('/grill/s/tok_with_query?ref=link')).toEqual({
      mode: 'grill',
      token: 'tok_with_query',
    });
    expect(parseRoute('/grill/sec_tok_12345')).toEqual({
      mode: 'grill',
      token: 'sec_tok_12345',
    });
  });
});

describe('Grill Intake File Whitelist', () => {
  it('allows safe document and image formats', () => {
    expect(isAllowedGrillExtension('spec.pdf')).toBe(true);
    expect(isAllowedGrillExtension('diagram.png')).toBe(true);
    expect(isAllowedGrillExtension('photo.jpg')).toBe(true);
    expect(isAllowedGrillExtension('photo.jpeg')).toBe(true);
    expect(isAllowedGrillExtension('render.webp')).toBe(true);
    expect(isAllowedGrillExtension('sensor.gif')).toBe(true);
    expect(isAllowedGrillExtension('notes.md')).toBe(true);
    expect(isAllowedGrillExtension('params.txt')).toBe(true);
  });

  it('rejects forbidden or executable formats', () => {
    expect(isAllowedGrillExtension('malicious.exe')).toBe(false);
    expect(isAllowedGrillExtension('script.py')).toBe(false);
    expect(isAllowedGrillExtension('archive.zip')).toBe(false);
    expect(isAllowedGrillExtension('data.tar.gz')).toBe(false);
    expect(isAllowedGrillExtension('binary.sh')).toBe(false);
  });
});

describe('Grill Question & Answer Contract', () => {
  it('conforms to 3 options, free-text, and unknown format', () => {
    const q: GrillQuestion = {
      id: 'q_payload',
      text: 'What is the required payload capacity?',
      why: 'Determine robot motor torque requirements',
      options: [
        { label: '< 5kg', interpretation: 'Light payload' },
        { label: '5-15kg', interpretation: 'Medium payload' },
        { label: '> 15kg', interpretation: 'Heavy payload' },
      ],
      free_text: true,
      allow_unknown: true,
    };

    expect(q.options).toHaveLength(3);
    expect(q.free_text).toBe(true);
    expect(q.allow_unknown).toBe(true);

    // Test answer serialization
    const ansOption: CustomerAnswer = {
      question_id: q.id,
      selected_option: '< 5kg',
      free_text_answer: null,
      is_unknown: false,
    };
    expect(ansOption.selected_option).toBe('< 5kg');

    const ansUnknown: CustomerAnswer = {
      question_id: q.id,
      selected_option: null,
      free_text_answer: null,
      is_unknown: true,
    };
    expect(ansUnknown.is_unknown).toBe(true);

    const ansFree: CustomerAnswer = {
      question_id: q.id,
      selected_option: null,
      free_text_answer: 'Exactly 7.2kg with custom clamp',
      is_unknown: false,
    };
    expect(ansFree.free_text_answer).toContain('7.2kg');
  });
});

describe('Grill Markdown Report Generation', () => {
  const mockSession: GrillSession = {
    id: 'gsess_test123',
    status: 'completed',
    task_intent: 'Carry warehouse pallet with B2 quadruped',
    referenced_robot: 'Unitree B2',
    question_count: 24,
    current_revision: 5,
    scenario_state: {
      revision: 5,
      summary: 'Carry warehouse pallet',
      root_id: 'n_root',
      nodes: [
        { id: 'n_root', name: 'CarryPalletSequence', type: 'sequence', children: ['n_nav', 'n_lift'] },
        { id: 'n_nav', name: 'NavigateToPallet', type: 'action', parent_id: 'n_root' },
        { id: 'n_lift', name: 'LiftPallet', type: 'action', parent_id: 'n_root' },
      ],
    },
    active_questions: [],
    readback_summary: 'Pallet mass 8kg, Unitree B2 quadruped navigation.',
    final_report: null,
    created_at: '2026-09-16T12:00:00Z',
    updated_at: '2026-09-16T12:15:00Z',
    finished_at: '2026-09-16T12:15:30Z',
    error_message: null,
  };

  const mockReport: GrillReport = {
    schema_version: '1.0',
    session_id: 'gsess_test123',
    scenario_summary: 'Pallet mass 8kg, Unitree B2 quadruped navigation.',
    capabilities: {
      summary: 'Payload feasible within 15kg dynamic limit.',
      claims: [
        {
          claim_id: 'c_payload',
          title: 'Payload capacity',
          category: 'payload',
          status: 'verified',
          statement: '8kg is within B2 20kg max payload.',
          citations: ['unitree_b2_spec.pdf'],
        },
      ],
    },
    system_architecture: {
      summary: 'Nav2 + CycloneDDS architecture.',
      nodes: [
        {
          name: 'pallet_navigator',
          package: 'b2_navigation',
          type: 'lifecycle',
          topics_sub: ['/scan', '/odom'],
          topics_pub: ['/cmd_vel'],
        },
      ],
      middleware: 'ROS 2 Humble / CycloneDDS',
      recommendations: ['Enable 3D lidar filtering'],
    },
    risk_matrix: {
      summary: 'Dynamic obstacle collision risk identified.',
      risks: [
        {
          risk_id: 'r_collision',
          title: 'Human worker cross-traffic',
          severity: 'medium',
          likelihood: 'medium',
          mitigation: 'Safety speed cap at 0.8m/s and audible warning chime.',
          citations: ['iso_3691_4.pdf'],
        },
      ],
    },
  };

  it('generates well-structured Markdown report with all 4 specialist sections', () => {
    const md = generateGrillMarkdown(mockReport, mockSession);

    expect(md).toContain('# Robot Scenario Assessment: Carry warehouse pallet with B2 quadruped');
    expect(md).toContain('**Target Robot:** Unitree B2');
    expect(md).toContain('**Questions Answered:** 24 / 30');
    expect(md).toContain('## 1. Scenario Summary');
    expect(md).toContain('## 2. Capabilities Assessment');
    expect(md).toContain('**[VERIFIED]** Payload capacity');
    expect(md).toContain('## 3. Integration Architecture');
    expect(md).toContain('pallet_navigator');
    expect(md).toContain('## 4. Operational Risk Matrix');
    expect(md).toContain('Human worker cross-traffic');
  });
});
