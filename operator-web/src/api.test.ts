import {afterEach, describe, expect, it, vi} from 'vitest';
import {api, ApiError, packedQuantity} from './api';
afterEach(()=>vi.unstubAllGlobals());
describe('API confirmations',()=>{
  it('sends the exact idempotency key, CSRF and revision on retry',async()=>{
    const fetchMock=vi.fn().mockResolvedValue(new Response(JSON.stringify({version:3}),{status:200}));
    vi.stubGlobal('fetch',fetchMock);
    await api('/fulfillments/one/pick',{version:2,serial:'A'},'csrf','request-1');
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/fulfillments/one/pick',expect.objectContaining({method:'POST',cache:'no-store',credentials:'same-origin',headers:expect.objectContaining({'X-CSRF-Token':'csrf','Idempotency-Key':'request-1'}),body:JSON.stringify({version:2,serial:'A'})}));
  });
  it('does not turn a rejected scan into a success',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({message:'Wrong serial'}),{status:409})));
    await expect(api('/scan',{})).rejects.toEqual(new ApiError('Wrong serial',409));
  });
  it('keeps repeated SKU lines separate when displaying packed quantities',()=>{
    const work={packages:[{contents:[{line_id:'1',quantity:'2'},{line_id:'2',quantity:'5'}]},{contents:[{line_id:'1',quantity:'1'}]}]};
    expect(packedQuantity(work,'1')).toBe(3);
    expect(packedQuantity(work,'2')).toBe(5);
  });
});
