/// <reference types="vite/client" />

import nextcloudSvg from '../public/brand/nextcloud.svg?raw';
import { describe, expect, it } from 'vitest';

describe('Nextcloud provider logo', () => {
  it('uses the standalone official blue mark', () => {
    const svg = new DOMParser().parseFromString(nextcloudSvg, 'image/svg+xml');
    const mark = svg.querySelector('path');
    const pathData = mark?.getAttribute('d') ?? '';

    expect(svg.documentElement.getAttribute('viewBox')).toBe('0 0 489 232');
    expect(mark?.getAttribute('fill')).toBe('#0082C9');
    expect(pathData).toContain('M244.8,7');
    expect(pathData).toContain('M76.3,86.3');
    expect(pathData).toContain('M412.5,86.3');
    expect(svg.querySelector('text')).toBeNull();
  });
});
