function addField(){document.getElementById('fields').insertAdjacentHTML('beforeend','<div class="row"><input name="field_name" placeholder="Field name"><select name="field_type"><option>text</option><option>number</option><option>decimal</option><option>date</option></select><input name="field_unit" placeholder="unit"><button type="button" onclick="this.parentElement.remove()">×</button></div>')}
function addAddon(){document.getElementById('addons').insertAdjacentHTML('beforeend','<div class="row"><input name="addon_name" placeholder="Add-on name"><input name="addon_price" type="number" step="0.01" placeholder="Price"><button type="button" onclick="this.parentElement.remove()">×</button></div>')}
function addItem(){let t=document.querySelector('.sale-item');let c=t.cloneNode(true);c.querySelectorAll('input').forEach((x,i)=>x.value=i===1?'1':i===2?'':'0');c.querySelector('select').selectedIndex=0;document.getElementById('items').appendChild(c)}
function fillPrice(s){let o=s.options[s.selectedIndex];let row=s.closest('.sale-item');row.querySelector('[name="unit_price"]').value=o.dataset.price||''}

document.querySelectorAll('input[type="password"]').forEach(input=>{
	const wrapper=document.createElement('span');
	wrapper.className='password-input-wrap';
	input.parentNode.insertBefore(wrapper,input);
	wrapper.appendChild(input);

	const toggle=document.createElement('button');
	toggle.type='button';
	toggle.className='password-visibility-toggle';
	toggle.textContent='Show';
	toggle.setAttribute('aria-label','Show password');
	toggle.setAttribute('aria-pressed','false');
	toggle.addEventListener('click',()=>{
		const show=input.type==='password';
		input.type=show?'text':'password';
		toggle.textContent=show?'Hide':'Show';
		toggle.setAttribute('aria-label',show?'Hide password':'Show password');
		toggle.setAttribute('aria-pressed',String(show));
	});
	wrapper.appendChild(toggle);
});
